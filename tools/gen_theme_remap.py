"""Regenerate the Tailwind colour-remap block inside kagger_theme.css.

Pages declare the theme they were designed for (kg-native-light / kg-native-dark).
In the *other* theme, the rules generated here re-colour the Tailwind colour
utilities those pages use, so most of the page follows the theme without
touching its markup. Page-specific CSS handles everything else.

Only classes that actually occur in the site's HTML/JS/Python are emitted, which
keeps the stylesheet small. Run this after adding new Tailwind colour classes:

    venv/Scripts/python.exe tools/gen_theme_remap.py
"""
import os
import re
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS = os.path.join(ROOT, 'kagger_theme.css')
START, END = '/* @@REMAP-START@@ */', '/* @@REMAP-END@@ */'
CLASS_RE = re.compile(r'(?:hover:|focus:|group-hover:)?(?:bg|text|border|divide|placeholder|from|via|to)-[a-z]+(?:-[0-9]{2,3})?(?:/(?:\[[0-9.]+\]|[0-9]+))?')


def used_classes():
    files = subprocess.run(['git', 'ls-files'], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    used = set()
    for f in files:
        if not f.endswith(('.html', '.js', '.py')) or f.startswith(('banthiya-astro-source/', 'astro_reports/', 'tests/')):
            continue
        try:
            with open(os.path.join(ROOT, f), encoding='utf-8', errors='ignore') as fh:
                used.update(CLASS_RE.findall(fh.read()))
        except OSError:
            pass
    return used

HUES = {  # 500 rgb, and text shades 300,400,600,700
    'red': ('239 68 68', '#fca5a5', '#f87171', '#dc2626', '#b91c1c'),
    'orange': ('249 115 22', '#fdba74', '#fb923c', '#ea580c', '#c2410c'),
    'amber': ('245 158 11', '#fcd34d', '#fbbf24', '#d97706', '#b45309'),
    'yellow': ('234 179 8', '#fde047', '#facc15', '#ca8a04', '#a16207'),
    'lime': ('132 204 22', '#bef264', '#a3e635', '#65a30d', '#4d7c0f'),
    'green': ('34 197 94', '#86efac', '#4ade80', '#16a34a', '#15803d'),
    'emerald': ('16 185 129', '#6ee7b7', '#34d399', '#059669', '#047857'),
    'teal': ('20 184 166', '#5eead4', '#2dd4bf', '#0d9488', '#0f766e'),
    'cyan': ('6 182 212', '#67e8f9', '#22d3ee', '#0891b2', '#0e7490'),
    'sky': ('14 165 233', '#7dd3fc', '#38bdf8', '#0284c7', '#0369a1'),
    'blue': ('59 130 246', '#93c5fd', '#60a5fa', '#2563eb', '#1d4ed8'),
    'indigo': ('99 102 241', '#a5b4fc', '#818cf8', '#4f46e5', '#4338ca'),
    'violet': ('139 92 246', '#c4b5fd', '#a78bfa', '#7c3aed', '#6d28d9'),
    'purple': ('168 85 247', '#d8b4fe', '#c084fc', '#9333ea', '#7e22ce'),
    'fuchsia': ('217 70 239', '#f0abfc', '#e879f9', '#c026d3', '#a21caf'),
    'pink': ('236 72 153', '#f9a8d4', '#f472b6', '#db2777', '#be185d'),
    'rose': ('244 63 94', '#fda4af', '#fb7185', '#e11d48', '#be123c'),
}
LIGHT_ON_WHITE_NEEDS_700 = {'green', 'emerald', 'teal', 'cyan', 'sky', 'lime', 'yellow', 'amber', 'orange'}
NEUTRALS = ['slate', 'gray', 'zinc', 'neutral', 'stone']
ALPHAS = ['5', '10', '20', '25', '30', '40', '50', '60', '70', '75', '80', '90', '95']


def esc(c):
    for a, b in (('/', '\\/'), (':', '\\:'), ('[', '\\['), (']', '\\]'), ('#', '\\#'), ('.', '\\.')):
        c = c.replace(a, b)
    return c


PRE = {'D': '[data-theme="dark"] :is(.kg-native-light, .kg-native-light *)',
       'L': '[data-theme="light"] :is(.kg-native-dark, .kg-native-dark *)'}


USED = used_classes()


def rule(scope, classes, decl, pseudo=''):
    classes = [c for c in classes if c in USED]
    if not classes:
        return ''
    if pseudo.startswith(':') and not pseudo.startswith('::'):
        # state pseudo-class goes on each class inside :is()
        return PRE[scope] + ':is(' + ', '.join('.' + esc(c) + pseudo for c in classes) + ') { ' + decl + ' }\n'
    return PRE[scope] + ':is(' + ', '.join('.' + esc(c) for c in classes) + ')' + pseudo + ' { ' + decl + ' }\n'


def with_alpha(base):
    return [base] + [base + '/' + a for a in ALPHAS]


out = []
A = out.append
# ------------------------------------------------------------- light-native -> dark
A('/* ---------- light-native content shown in DARK theme ---------- */\n')
A(rule('D', ['bg-white'], 'background-color: var(--kg-surface);'))
A(rule('D', ['bg-%s-50' % n for n in NEUTRALS], 'background-color: var(--kg-subtle);'))
A(rule('D', ['bg-%s-100' % n for n in NEUTRALS], 'background-color: var(--kg-inset);'))
A(rule('D', ['bg-%s-200' % n for n in NEUTRALS], 'background-color: var(--kg-raised);'))
A(rule('D', ['bg-%s-300' % n for n in NEUTRALS], 'background-color: var(--kg-raised-2);'))
A(rule('D', ['bg-white/' + a for a in ['50', '60', '70', '75', '80', '90', '95']], 'background-color: rgb(22 22 31 / .88);'))
A(rule('D', ['hover:bg-white'] + ['hover:bg-%s-%s' % (n, s) for n in NEUTRALS for s in ('50', '100', '200')],
       'background-color: var(--kg-surface-hover);', ':hover'))
A(rule('D', ['text-%s-%s' % (n, s) for n in NEUTRALS for s in ('900', '800', '950')] + ['text-black'], 'color: var(--kg-text);'))
A(rule('D', ['text-%s-700' % n for n in NEUTRALS], 'color: var(--kg-text-2);'))
A(rule('D', ['text-%s-600' % n for n in NEUTRALS], 'color: var(--kg-text-3);'))
A(rule('D', ['text-%s-500' % n for n in NEUTRALS], 'color: var(--kg-muted);'))
A(rule('D', ['hover:text-%s-%s' % (n, s) for n in NEUTRALS for s in ('700', '800', '900')], 'color: var(--kg-text);', ':hover'))
A(rule('D', ['border-%s-%s' % (n, s) for n in NEUTRALS for s in ('50', '100', '200', '300')], 'border-color: var(--kg-border);'))
A(rule('D', ['hover:border-%s-%s' % (n, s) for n in NEUTRALS for s in ('200', '300', '400')],
       'border-color: var(--kg-border-strong);', ':hover'))
A(rule('D', ['divide-%s-%s' % (n, s) for n in NEUTRALS for s in ('100', '200')], 'border-color: var(--kg-border);',
       ' > :not([hidden]) ~ :not([hidden])'))
for g, (rgb, t3, t4, t6, t7) in HUES.items():
    A(rule('D', ['bg-%s-50' % g], 'background-color: rgb(%s / .10);' % rgb))
    A(rule('D', ['bg-%s-100' % g], 'background-color: rgb(%s / .16);' % rgb))
    A(rule('D', ['bg-%s-200' % g], 'background-color: rgb(%s / .24);' % rgb))
    A(rule('D', ['hover:bg-%s-50' % g, 'hover:bg-%s-100' % g], 'background-color: rgb(%s / .20);' % rgb, ':hover'))
    A(rule('D', ['text-%s-%s' % (g, s) for s in ('600', '700', '800', '900', '950')], 'color: %s;' % t4))
    A(rule('D', ['hover:text-%s-%s' % (g, s) for s in ('600', '700', '800', '900')], 'color: %s;' % t3, ':hover'))
    A(rule('D', ['border-%s-%s' % (g, s) for s in ('50', '100', '200', '300')], 'border-color: rgb(%s / .32);' % rgb))
    A(rule('D', ['from-%s-50' % g, 'from-%s-100' % g], '--tw-gradient-from: rgb(%s / .12) var(--tw-gradient-from-position);' % rgb))
    A(rule('D', ['via-%s-50' % g, 'via-%s-100' % g],
           '--tw-gradient-stops: var(--tw-gradient-from), rgb(%s / .10) var(--tw-gradient-via-position), var(--tw-gradient-to);' % rgb))
    A(rule('D', ['to-%s-50' % g, 'to-%s-100' % g], '--tw-gradient-to: rgb(%s / .06) var(--tw-gradient-to-position);' % rgb))
A(rule('D', ['from-white'] + ['from-%s-%s' % (n, s) for n in NEUTRALS for s in ('50', '100')],
       '--tw-gradient-from: var(--kg-surface) var(--tw-gradient-from-position);'))
A(rule('D', ['via-white'] + ['via-%s-%s' % (n, s) for n in NEUTRALS for s in ('50', '100')],
       '--tw-gradient-stops: var(--tw-gradient-from), var(--kg-surface) var(--tw-gradient-via-position), var(--tw-gradient-to);'))
A(rule('D', ['to-white'] + ['to-%s-%s' % (n, s) for n in NEUTRALS for s in ('50', '100')],
       '--tw-gradient-to: var(--kg-subtle) var(--tw-gradient-to-position);'))

# ------------------------------------------------------------- dark-native -> light
A('\n/* ---------- dark-native content shown in LIGHT theme ---------- */\n')
A(rule('L', sum([with_alpha('bg-%s-%s' % (n, s)) for n in NEUTRALS for s in ('900', '950')], []) + ['bg-black'],
       'background-color: var(--kg-subtle);'))
A(rule('L', sum([with_alpha('bg-%s-800' % n) for n in NEUTRALS], []), 'background-color: var(--kg-surface);'))
A(rule('L', sum([with_alpha('bg-%s-700' % n) for n in NEUTRALS], []), 'background-color: var(--kg-inset);'))
A(rule('L', sum([with_alpha('bg-%s-600' % n) for n in NEUTRALS], []), 'background-color: var(--kg-raised);'))
A(rule('L', ['bg-white/5', 'bg-white/[0.03]', 'bg-white/[0.02]'], 'background-color: rgb(15 23 42 / .03);'))
A(rule('L', ['bg-white/10'], 'background-color: rgb(15 23 42 / .05);'))
A(rule('L', ['bg-white/20', 'bg-white/15'], 'background-color: rgb(15 23 42 / .08);'))
A(rule('L', ['bg-black/10', 'bg-black/20', 'bg-black/25', 'bg-black/30', 'bg-black/35', 'bg-black/40'],
       'background-color: rgb(15 23 42 / .04);'))
A(rule('L', ['hover:bg-white/5', 'hover:bg-white/10', 'hover:bg-white/20'] +
       ['hover:bg-%s-%s' % (n, s) for n in NEUTRALS for s in ('600', '700', '800', '900')] +
       ['hover:bg-%s-%s/50' % (n, s) for n in NEUTRALS for s in ('700', '800')],
       'background-color: var(--kg-surface-hover);', ':hover'))
A(rule('L', ['text-white'] + ['text-%s-%s' % (n, s) for n in NEUTRALS for s in ('50', '100', '200')], 'color: var(--kg-text);'))
A(rule('L', ['text-%s-300' % n for n in NEUTRALS], 'color: var(--kg-text-2);'))
A(rule('L', ['text-%s-400' % n for n in NEUTRALS], 'color: var(--kg-text-3);'))
A(rule('L', ['text-%s-500' % n for n in NEUTRALS], 'color: var(--kg-muted);'))
A(rule('L', ['text-%s-%s' % (n, s) for n in NEUTRALS for s in ('600', '700')], 'color: var(--kg-faint);'))
A(rule('L', ['text-white/' + a for a in ('40', '50', '60', '70', '80', '90')], 'color: var(--kg-text-3);'))
A(rule('L', ['hover:text-white'] + ['hover:text-%s-%s' % (n, s) for n in NEUTRALS for s in ('100', '200', '300')],
       'color: var(--kg-text-strong);', ':hover'))
A(rule('L', sum([with_alpha('border-%s-%s' % (n, s)) for n in NEUTRALS for s in ('600', '700', '800', '900')], []) +
       ['border-white/5', 'border-white/10', 'border-white/20', 'border-white/[0.06]', 'border-white/[0.08]'],
       'border-color: var(--kg-border);'))
A(rule('L', with_alpha('border-slate-500') + ['border-white/30'], 'border-color: var(--kg-border-strong);'))
A(rule('L', ['hover:border-%s-%s' % (n, s) for n in NEUTRALS for s in ('500', '600')] + ['hover:border-white/20', 'hover:border-white/30'],
       'border-color: var(--kg-border-strong);', ':hover'))
A(rule('L', ['divide-%s-%s' % (n, s) for n in NEUTRALS for s in ('700', '800')] + ['divide-white/5', 'divide-white/10'],
       'border-color: var(--kg-border);', ' > :not([hidden]) ~ :not([hidden])'))
A(rule('L', ['placeholder-%s-%s' % (n, s) for n in NEUTRALS for s in ('400', '500', '600')], 'color: var(--kg-faint);', '::placeholder'))
A(rule('L', ['from-%s-%s' % (n, s) for n in NEUTRALS for s in ('700', '800', '900', '950')] +
       ['from-%s-%s/%s' % (n, s, a) for n in ('slate', 'gray') for s in ('800', '900') for a in ('50', '60', '80', '90')] + ['from-black'],
       '--tw-gradient-from: var(--kg-surface) var(--tw-gradient-from-position); --tw-gradient-to: rgb(255 255 255 / 0) var(--tw-gradient-to-position); --tw-gradient-stops: var(--tw-gradient-from), var(--tw-gradient-to);'))
A(rule('L', ['via-%s-%s' % (n, s) for n in NEUTRALS for s in ('800', '900', '950')],
       '--tw-gradient-stops: var(--tw-gradient-from), var(--kg-surface) var(--tw-gradient-via-position), var(--tw-gradient-to);'))
A(rule('L', ['to-%s-%s' % (n, s) for n in NEUTRALS for s in ('700', '800', '900', '950')] +
       ['to-%s-%s/%s' % (n, s, a) for n in ('slate', 'gray') for s in ('800', '900') for a in ('50', '60', '80', '90')] + ['to-black'],
       '--tw-gradient-to: var(--kg-subtle) var(--tw-gradient-to-position);'))
for g, (rgb, t3, t4, t6, t7) in HUES.items():
    target = t7 if g in LIGHT_ON_WHITE_NEEDS_700 else t6
    A(rule('L', ['text-%s-%s' % (g, s) for s in ('100', '200', '300', '400')], 'color: %s;' % target))
    A(rule('L', ['hover:text-%s-%s' % (g, s) for s in ('200', '300', '400')], 'color: %s;' % t7, ':hover'))
    A(rule('L', sum([with_alpha('bg-%s-%s' % (g, s)) for s in ('800', '900', '950')], []), 'background-color: rgb(%s / .09);' % rgb))
    A(rule('L', sum([with_alpha('border-%s-%s' % (g, s)) for s in ('700', '800', '900')], []), 'border-color: rgb(%s / .35);' % rgb))
    A(rule('L', ['from-%s-%s/%s' % (g, s, a) for s in ('800', '900', '950') for a in ('10', '20', '30', '40', '50')] +
           ['from-%s-%s' % (g, s) for s in ('900', '950')],
           '--tw-gradient-from: rgb(%s / .08) var(--tw-gradient-from-position); --tw-gradient-to: rgb(%s / 0) var(--tw-gradient-to-position); --tw-gradient-stops: var(--tw-gradient-from), var(--tw-gradient-to);' % (rgb, rgb)))
    A(rule('L', ['to-%s-%s/%s' % (g, s, a) for s in ('800', '900', '950') for a in ('10', '20', '30', '40', '50')] +
           ['to-%s-%s' % (g, s) for s in ('900', '950')],
           '--tw-gradient-to: rgb(%s / .05) var(--tw-gradient-to-position);' % rgb))

# Restore white text on solid saturated fills (buttons, badges) in light theme.
solid = [c for c in ['bg-%s-%s' % (g, s) for g in HUES for s in ('500', '600', '700', '800')] if c in USED]
grad_from = [c for c in ['from-%s-%s' % (g, s) for g in HUES for s in ('500', '600', '700')] if c in USED]
pre = PRE['L']
solid_is = ':is(' + ', '.join('.' + esc(c) for c in solid) + ')'
grad_is = ':is(' + ', '.join('.' + esc(c) for c in grad_from) + ')'
on_white = ':is(.text-white, .text-slate-100, .text-slate-200, .text-slate-300, .text-gray-100, .text-gray-200)'
A('\n/* Solid saturated fills (buttons, badges) and .kg-on-color regions keep white text in light theme. */\n')
A('%s%s:not(.text-transparent),\n%s[class*="bg-gradient-to"]%s:not(.text-transparent):not(.bg-clip-text),\n'
  '[data-theme="light"] .kg-native-dark .kg-on-color { color: #fff; }\n' % (pre, solid_is, pre, grad_is))
A('%s%s %s,\n%s[class*="bg-gradient-to"]%s %s,\n[data-theme="light"] .kg-native-dark .kg-on-color %s { color: #fff; }\n'
  % (pre, solid_is, on_white, pre, grad_is, on_white, on_white))
block = ''.join(out)
with open(CSS, encoding='utf-8') as fh:
    css = fh.read()
i, j = css.index(START) + len(START), css.index(END)
css = css[:i] + '\n' + block + css[j:]
with open(CSS, 'w', encoding='utf-8', newline='\n') as fh:
    fh.write(css)
print('kagger_theme.css: remap block %d bytes, %d classes in use' % (len(block), len(USED)))
