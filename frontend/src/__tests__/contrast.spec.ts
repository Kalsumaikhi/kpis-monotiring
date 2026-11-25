import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

// Read directly: Vite's ?raw import of a .css file comes back empty under vitest.
// Vitest runs from the frontend/ directory (npm run test), so cwd is the project root.
const tokensCss = readFileSync(join(process.cwd(), 'src/styles/tokens.css'), 'utf8')

type Vars = Record<string, string>

const stripComments = (css: string) => css.replace(/\/\*[\s\S]*?\*\//g, '')

// Each rule body keyed by its selector. The @media wrapper is ignored: its inner rule is matched on its own.
function rules(css: string): { selector: string; vars: Vars }[] {
  const out: { selector: string; vars: Vars }[] = []
  for (const m of stripComments(css).matchAll(/(:root[^{]*)\{([^}]*)\}/g)) {
    const vars: Vars = {}
    for (const d of m[2].matchAll(/--([\w-]+)\s*:\s*([^;]+);/g)) vars[d[1]] = d[2].trim()
    out.push({ selector: m[1].trim(), vars })
  }
  return out
}

const lightRule = rules(tokensCss).find((r) => r.selector === ':root')!.vars
const darkMediaRule = rules(tokensCss).find((r) => r.selector.includes(":not([data-theme='light'])"))!.vars
const darkAttrRule = rules(tokensCss).find((r) => r.selector === ":root[data-theme='dark']")!.vars

// Resolve var(--x) references within one theme; the light :root map supplies anything a theme omits.
function resolve(name: string, theme: Vars, depth = 0): string {
  const raw = theme[name] ?? lightRule[name]
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
  const springGreen = resolve('c-spring-green', lightRule)
  const forbidden = ['text', 'accent', ...Object.keys(lightRule).filter((k) => k.startsWith('status-'))]

  it.each(forbidden)('light --%s is not spring green', (name) => {
    expect(resolve(name, lightRule)).not.toBe(springGreen)
  })
})
