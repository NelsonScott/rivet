#!/usr/bin/env bash
# Builds a source RPM from the committed HEAD:
#   packaging/rpm/out/rivet-updater-<ver>.tar.gz          git archive of HEAD
#   packaging/rpm/out/rivet-updater-<ver>-1.<dist>.src.rpm
# Then publish with:  copr-cli build rivet-updater packaging/rpm/out/rivet-updater-*.src.rpm
# Needs git and rpmbuild (rpm-build). Nothing is vendored: the backend is stdlib only.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="${ROOT}/packaging/rpm/out"
SPEC="${ROOT}/packaging/rpm/rivet-updater.spec"
NAME="rivet-updater"
VERSION="$(sed -n 's/^Version: *//p' "${SPEC}")"
meta_version="$(sed -n 's/.*<release version="\([^"]*\)".*/\1/p' "${ROOT}/data/xyz.scottnelson.Rivet.metainfo.xml" | head -1)"
[[ "${VERSION}" == "${meta_version}" ]] || { echo "spec ${VERSION} != metainfo ${meta_version}" >&2; exit 1; }
[[ -z "$(git -C "${ROOT}" status --porcelain)" ]] || echo "warning: uncommitted changes are NOT in the tarball" >&2

rm -rf "${OUT}"
mkdir -p "${OUT}"
git -C "${ROOT}" archive --format=tar.gz --prefix="${NAME}-${VERSION}/" -o "${OUT}/${NAME}-${VERSION}.tar.gz" HEAD

rpmbuild -bs "${SPEC}" \
    --define "_sourcedir ${OUT}" --define "_srcrpmdir ${OUT}" \
    --define "_builddir ${OUT}/build" --define "dist .fc$(rpm -E %fedora)"
ls -1 "${OUT}"/*.src.rpm
