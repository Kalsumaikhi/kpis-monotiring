import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

// Vitest injects __dirname for the module, so the path is relative to this file.
const tokensCss = readFileSync(join(__dirname, '../styles/tokens.css'), 'utf8')

type Vars = Record<string, string>
type Block = { selector: string; vars: Vars }

const stripComments = (css: string) => css.replace(/\/\*[\s\S]*?\*\//g, '')

// Every rule whose selector starts with :root, keyed by its trimmed selector. The @media wrapper is
// ignored: its inner rule is matched on its own. Each body must declare exactly the properties it
// contains, so a declaration without a trailing ';' fails loudly instead of being dropped.
function rules(css: string): Block[] {
  const out: Block[] = []
  for (const m of stripComments(css).matchAll(/(:root[^{]*)\{([^}]*)\}/g)) {
    const vars: Vars = {}
    for (const d of m[2].matchAll(/--([\w-]+)\s*:\s*([^;]+);/g)) vars[d[1]] = d[2].trim()
    const declared = (m[2].match(/--[\w-]+\s*:/g) ?? []).length
    if (declared !== Object.keys(vars).length) {
      throw new Error(`rule "${m[1].trim()}" has ${declared} declarations but ${Object.keys(vars).length} parse (missing ';'?)`)
    }
    out.push({ selector: m[1].trim(), vars })
  }
  return out
}

// Exactly one rule must match; a duplicated block would otherwise be silently ignored.
function single(blocks: Block[], label: string, match: (selector: string) => boolean): Vars {
  const hits = blocks.filter((b) => match(b.selector))
  if (hits.length !== 1) throw new Error(`expected exactly one ${label} block, found ${hits.length}`)
  return hits[0].vars
}

const all = rules(tokensCss)
const lightRule = single(all, 'light :root', (s) => s === ':root')
const darkMediaRule = single(all, 'dark media', (s) => s.includes(":not([data-theme='light'])"))
const darkAttrRule = single(all, 'dark [data-theme]', (s) => s === ":root[data-theme='dark']")

// Dark blocks must define every token the light block does; resolution never falls back to light.
for (const [label, dark] of [
  ['dark media', darkMediaRule],
  ['dark [data-theme]', darkAttrRule],
] as const) {
  const missing = Object.keys(lightRule).filter((k) => !(k in dark))
  if (missing.length) throw new Error(`${label} block does not define: ${missing.join(', ')}`)
}

// Resolve var(--x) references within one theme block.
function resolve(name: string, theme: Vars, depth = 0): string {
  const raw = theme[name]
  if (raw === undefined) throw new Error(`token --${name} is not defined`)
  if (depth > 10) throw new Error(`token --${name} has a reference cycle`)
  return raw.replace(/var\(--([\w-]+)\)/g, (_, ref: string) => resolve(ref, theme, depth + 1))
}

function luminance(hex: string): number {
  const m = /^#([0-9a-f]{6})$/i.exec(hex)
  if (!m) throw new Error(`expected an opaque #rrggbb colour, got "${hex}"`)
  const [r, g, b] = [0, 2, 4].map((i) => {
    const c = parseInt(m[1].slice(i, i + 2), 16) / 255
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
  })
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

// WCAG 2.x contrast ratio.
function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x)
  return (hi + 0.05) / (lo + 0.05)
}

const THRESHOLD = 4.5

// "fg/bg" pairs are token names without the leading "--".
const pairs = [
  'text/bg',
  'text/surface',
  'text-muted/surface',
  'accent/surface',
  'link/bg',
  'link/surface',
  'status-ok/surface',
  'status-warn/surface',
  'status-fail/surface',
  'nav-text/nav-bg',
  'nav-active/nav-bg',
  'btn-text/btn-bg',
]

const themes: [string, Vars][] = [
  ['light', lightRule],
  ['dark', darkMediaRule],
]

describe('token contrast (WCAG AA, 4.5:1 for text)', () => {
  it.each(themes.flatMap(([theme, vars]) => pairs.map((p) => [theme, p, vars] as const)))(
    '%s theme: %s',
    (_theme, pair, vars) => {
      const [fg, bg] = pair.split('/')
      const ratio = contrast(resolve(fg, vars), resolve(bg, vars))
      expect(ratio, `${pair} = ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(THRESHOLD)
    },
  )

  it('dark theme blocks define identical values', () => {
    expect(darkAttrRule).toEqual(darkMediaRule)
  })
})

describe('spring green is only an accent on dark surfaces', () => {
  const springGreen = resolve('c-spring-green', lightRule).toLowerCase()
  const forbidden = [
    'text',
    'accent',
    'status-ok',
    'status-warn',
    'status-fail',
    'chg-added',
    'chg-altered',
    'chg-removed',
  ]

  it.each(forbidden)('light --%s is not spring green', (name) => {
    expect(resolve(name, lightRule).toLowerCase()).not.toBe(springGreen)
  })
})
