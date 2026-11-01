#!/usr/bin/env python3
"""Contrast check of every look's tokens (app/ui/looks/*.css over style.css), light + dark.

    python3 app/qa/looks-contrast.py            # exit 1 if any new look has a pair under 4.5:1

Resolves the custom properties the way the cascade does (style.css :root + theme, then the
look's root block, then its theme block; the Poster top band adds its scoped block), composites
translucent tints over the surface they sit on, and checks the text/surface pairs the front page
actually paints. Periwinkle (style.css as-is) is reported but never fails the run: it predates
the looks and is kept byte-for-byte."""
import os, re, sys

UI = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'ui')

def blocks(css):
    css = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    for m in re.finditer(r'([^{}]+)\{([^{}]*)\}', css):
        sel = ' '.join(m.group(1).split())
        props = dict((k.strip(), v.strip()) for k, v in re.findall(r'(--[\w-]+)\s*:\s*([^;]+)', m.group(2)))
        if props:
            yield sel, props

def parse_color(v):
    v = v.strip()
    m = re.search(r'#([0-9a-fA-F]{3,8})\b|rgba?\(([^)]*)\)|\btransparent\b', v)
    if not m:
        return None
    if m.group(0) == 'transparent':
        return (0, 0, 0, 0.0)
    if m.group(1):
        h = m.group(1)
        if len(h) == 3:
            h = ''.join(c * 2 for c in h)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), int(h[6:8], 16) / 255 if len(h) == 8 else 1.0)
    p = [x.strip() for x in m.group(2).split(',')]
    return (float(p[0]), float(p[1]), float(p[2]), float(p[3]) if len(p) > 3 else 1.0)

def over(top, under):
    a = top[3]
    return tuple(top[i] * a + under[i] * (1 - a) for i in range(3)) + (1.0,)

def lum(c):
    def ch(x):
        x /= 255
        return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(c[0]) + 0.7152 * ch(c[1]) + 0.0722 * ch(c[2])

def ratio(a, b):
    la, lb = lum(a), lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)

def tokens(look, theme, scope):
    t = {}
    for sel, p in blocks(open(os.path.join(UI, 'style.css')).read()):
        if sel == ':root' or sel == ':root[data-theme="%s"]' % theme:
            t.update(p)
    t.setdefault('--chrome', t['--bg'])
    t.setdefault('--on-accent', '#ffffff')
    t.setdefault('--on-red-solid', '#ffffff')
    if look != 'periwinkle':
        css = open(os.path.join(UI, 'looks', look + '.css')).read()
        root = ':root[data-look="%s"]' % look
        for want in (root, root + '[data-theme="%s"]' % theme):
            for sel, p in blocks(css):
                if sel == want:
                    t.update(p)
        if scope == 'chrome':
            for sel, p in blocks(css):
                if sel.startswith(root + ' ') and '.header' in sel:
                    t.update(p)
    return t

def check(look, theme):
    rows = []
    for scope in ('main', 'chrome'):
        t = tokens(look, theme, scope)
        C = lambda k: parse_color(t[k])
        def pair(fg, bgs, label):
            # bgs: surface token first, then translucent layers painted over it
            b = C(bgs[0])
            if b[3] < 1:                    # a translucent surface (Periwinkle's cards) sits on --bg
                b = over(b, C('--bg'))
            for k in bgs[1:]:
                b = over(C(k), b)
            f = C(fg)
            if f[3] < 1:
                f = over(f, b)
            rows.append((scope, label, ratio(f, b)))
        if scope == 'main':
            for s in ('--card', '--bg'):
                for f in ('--fg', '--fg-2', '--fg-3', '--fg-4', '--accent-text', '--yellow', '--red-text', '--green'):
                    pair(f, [s], '%s on %s' % (f, s))
            pair('--fg-2', ['--card', '--field'], 'source tag (--fg-2 on --field)')
            pair('--yellow', ['--card', '--yellow-bg'], 'minor badge (--yellow on --yellow-bg)')
            pair('--yellow', ['--card', '--yellow-banner'], 'warning banner')
            pair('--red-text', ['--card', '--red-bg'], 'major badge (--red-text on --red-bg)')
            pair('--red-text', ['--card', '--red-banner'], 'red banner')
            pair('--green', ['--card', '--green-bg'], 'patch badge (--green on --green-bg)')
            pair('--on-accent', ['--grad'], 'primary button (--on-accent on --grad)')
            pair('--on-accent', ['--accent'], 'checkbox / segmented (--on-accent on --accent)')
            pair('--on-red-solid', ['--red-solid'], 'CHECK FAILED badge (--on-red-solid on --red-solid)')
            pair('--log-fg', ['--log-bg'], 'log text')
            pair('--fg', ['--bg', '--field'], 'text in fields')
            # 2026-11-01 abuse pass: the failed / busy / Fix it states of an Other-software row,
            # the "Install Claude Code" note, and the health-check banner (backend/health.py)
            pair('--red-text', ['--card', '--red-banner'], 'failed row line (.ew-run.fail)')
            pair('--fg', ['--card', '--red-banner', '--btn-bg'], 'Fix it button on a failed row')
            pair('--red-text', ['--card', '--red-banner', '--btn-bg'], 'Remove (.btn.danger) on a failed row')
            pair('--fg', ['--card', '--field'], 'busy row line (.ew-run.running)')
            pair('--log-fg', ['--log-bg'], 'busy / failed row output (.ew-log)')
            pair('--green', ['--card', '--green-bg'], 'done row line (.ew-run.ok)')
            pair('--fg-2', ['--card'], 'Install Claude Code note on a row (.no-claude)')
            pair('--fg-2', ['--bg'], 'Install Claude Code note on the result screen')
            pair('--fg', ['--bg', '--yellow-banner'], 'health banner title')
            pair('--fg-2', ['--bg', '--yellow-banner'], 'health banner detail / busy line')
            pair('--yellow', ['--bg', '--yellow-banner', '--yellow-bg'], 'health banner glyph')
            pair('--red-text', ['--bg', '--red-banner'], 'health banner after a failed fix')
            pair('--on-accent', ['--grad'], 'health banner fix button')
        has_chrome_scope = scope == 'chrome' and look == 'poster'
        if (scope == 'main' and look != 'poster') or has_chrome_scope:
            for f in ('--fg', '--fg-2', '--fg-3', '--accent-text', '--red-text', '--yellow'):
                pair(f, ['--chrome'], 'top band: %s on --chrome' % f)
            pair('--green', ['--chrome', '--green-bg'], 'top band: ok glyph')
        if has_chrome_scope:
            pair('--on-accent', ['--grad'], 'top band: primary button')
    return rows

def main():
    looks = ['periwinkle'] + sorted(f[:-4] for f in os.listdir(os.path.join(UI, 'looks')) if f.endswith('.css'))
    bad = 0
    for look in looks:
        for theme in ('light', 'dark'):
            rows = check(look, theme)
            worst = min(rows, key=lambda r: r[2])
            fails = [r for r in rows if r[2] < 4.5]
            tag = 'info' if look == 'periwinkle' else ('FAIL' if fails else 'ok')
            print('%-10s %-5s %-4s worst %.2f (%s)' % (look, theme, tag, worst[2], worst[1]))
            for s, l, r in fails:
                print('    %.2f  %s' % (r, l))
            if look != 'periwinkle':
                bad += len(fails)
    print('LOOKS CONTRAST PASS' if not bad else 'LOOKS CONTRAST FAIL (%d pairs under 4.5:1)' % bad)
    return 1 if bad else 0

if __name__ == '__main__':
    sys.exit(main())
