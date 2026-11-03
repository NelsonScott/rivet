# Sourced by every QA script that starts a nested headless gnome-shell.
#
# A nested session that shares the live XDG_RUNTIME_DIR activates a second
# xdg-document-portal on its private bus. On start that instance finds the live
# FUSE mount at $XDG_RUNTIME_DIR/doc, lazily unmounts it and mounts its own; on
# exit it removes its own, and nothing restores the first. From then on every
# new Flatpak launch fails in bwrap ("Can't find source path .../doc/by-app").
# So the whole run gets a private runtime dir and its own bus: the second portal
# only ever touches that dir. Re-executes the calling script once; the caller's
# own `dbus-run-session` wrapper, if any, is harmless.
if [ -z "${RIVET_QA_RUNTIME_DIR:-}" ]; then
  RIVET_QA_RUNTIME_DIR=$(mktemp -d "${TMPDIR:-/tmp}/rivet-qa-rt.XXXXXX")
  export RIVET_QA_RUNTIME_DIR
  qa_runtime_cleanup() {
    # The private portal exports the real HOME's documents, and deleting inside
    # its root view deletes the real file: detach the mount first, and never
    # let rm cross a mount boundary.
    fusermount3 -u -z "$RIVET_QA_RUNTIME_DIR/doc" 2>/dev/null
    mountpoint -q "$RIVET_QA_RUNTIME_DIR/doc" || rm -rf --one-file-system "$RIVET_QA_RUNTIME_DIR"
  }
  trap qa_runtime_cleanup EXIT
  XDG_RUNTIME_DIR="$RIVET_QA_RUNTIME_DIR" dbus-run-session -- bash "$0" "$@"
  exit $?
fi
