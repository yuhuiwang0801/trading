// Display formatting only. Never use these strings for calculations or orders.
const numeric = (value: unknown): number | null => {
  if (value == null || typeof value === 'boolean' || (typeof value !== 'number' && typeof value !== 'string') || String(value).trim() === '') return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}
const digits = (value: number) => Number.isInteger(value) ? 0 : 2

export function formatNumber(value: unknown): string {
  const number = numeric(value)
  return number === null ? '—' : new Intl.NumberFormat('en-US', { minimumFractionDigits: digits(number), maximumFractionDigits: digits(number) }).format(number)
}

export function formatMoney(value: unknown): string {
  const number = numeric(value)
  return number === null ? '—' : new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: digits(number), maximumFractionDigits: digits(number) }).format(number)
}

export function formatPercent(value: unknown, scale = 100): string {
  const number = numeric(value)
  // Remove multiplication noise such as 0.07 * 100 = 7.000000000000001.
  return number === null ? '—' : `${formatNumber(Number((number * scale).toPrecision(15)))}%`
}

export function formatCompact(value: unknown): string {
  const number = numeric(value)
  if (number === null) return '—'
  const unit = Math.abs(number) >= 1e12 ? 1e12 : Math.abs(number) >= 1e9 ? 1e9 : Math.abs(number) >= 1e6 ? 1e6 : Math.abs(number) >= 1e3 ? 1e3 : 1
  const precision = digits(number / unit)
  return new Intl.NumberFormat('en-US', { notation: 'compact', minimumFractionDigits: precision, maximumFractionDigits: precision }).format(number)
}

const identityField = /(^|_)(id|ids|symbol|symbols|ticker|account|number|version|hash|date|time|at|timestamp)($|_)/
const decimalText = /^[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?$/i
const detailDecimal = (value: unknown, key: string): string | null => {
  if (identityField.test(key) || (typeof value !== 'number' && (typeof value !== 'string' || !decimalText.test(value)))) return null
  const number = numeric(value)
  if (number === null) return null
  if (!Number.isInteger(number)) return formatNumber(number).replaceAll(',', '')
  return typeof value === 'string' && /^[+-]?\d+\.0+$/.test(value) ? value.replace(/\.0+$/, '') : null
}

export function formatDetailValue(value: unknown, key: string): string {
  return value == null ? '—' : detailDecimal(value, key) ?? String(value)
}

// Keep JSON identities, dates, integers and types intact; round decimal display
// recursively without mutating the source snapshot or its exact numeric strings.
export function formatDetailJson(value: unknown, key = '', depth = 0): string {
  if (Array.isArray(value)) {
    if (!value.length) return '[]'
    return `[\n${value.map(item => `${'  '.repeat(depth + 1)}${formatDetailJson(item, key, depth + 1)}`).join(',\n')}\n${'  '.repeat(depth)}]`
  }
  if (value !== null && typeof value === 'object') {
    const entries = Object.entries(value).filter(([, item]) => item !== undefined)
    if (!entries.length) return '{}'
    return `{\n${entries.map(([name, item]) => `${'  '.repeat(depth + 1)}${JSON.stringify(name)}: ${formatDetailJson(item, name, depth + 1)}`).join(',\n')}\n${'  '.repeat(depth)}}`
  }
  const rounded = detailDecimal(value, key)
  return rounded !== null ? typeof value === 'string' ? JSON.stringify(rounded) : rounded : JSON.stringify(value) ?? 'null'
}
