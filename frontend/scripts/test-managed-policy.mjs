import assert from 'node:assert/strict'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'

const root = resolve(fileURLToPath(new URL('..', import.meta.url)))
const temp = await mkdtemp(join(tmpdir(), 'northstar-managed-policy-'))
try {
  const outfile = join(temp, 'test.cjs')
  await build({
    stdin: { contents: `import {createElement} from 'react';
      import {renderToStaticMarkup} from 'react-dom/server';
      import ManagedPolicy, {managedPolicyBlockers} from './src/ManagedPolicy';
      export {managedPolicyBlockers};
      export const render = props => renderToStaticMarkup(createElement(ManagedPolicy, props));`,
      resolveDir: root, sourcefile: 'test-managed-policy.ts', loader: 'ts' },
    bundle: true, outfile, format: 'cjs', platform: 'node', jsx: 'automatic', logLevel: 'silent',
  })
  const { render, managedPolicyBlockers } = createRequire(import.meta.url)(outfile)
  const policy = { policy_hash: 'fixed-policy', version: 'v1', universe_mode: 'fixed_vetted_universe',
    universe_count: 190, max_order_weight: '.05', max_daily_added_weight: '.20', max_positions: 20,
    max_entries_per_day: 4, max_industry_weight: '.25', max_issuer_weight: '.05' }
  const config = { scope_mode: 'strategy_managed', policy_hash: policy.policy_hash, policy_version: policy.version,
    universe_hash: 'universe', strategy_policy: policy, symbols: ['AAPL', 'NVDA'],
    max_order_amount: '50', max_daily_added: '200', current_buy_order_ceiling: '35' }
  const html = render({ language: 'zh', policy, config })
  for (const expected of ['190', '5%', '20%', '25%', '$50', '$200', '$35', '无需填写', '查看 2 只策略股票']) assert.ok(html.includes(expected), expected)
  assert.ok(!/<(?:input|select)\b/.test(html), 'Policy summary must be read-only')
  assert.ok(!/<button(?![^>]*type="button")/.test(html), 'Detail buttons must not submit the activation form')
  const unprepared = render({ language: 'zh', policy })
  assert.ok(unprepared.includes('自动计算对应金额'))
  assert.ok(!unprepared.includes('$0'), 'Unknown account caps must not become zero')
  const trend = { ...policy, universe_mode: 'eligible_active_us_equities', universe_count: null,
    max_order_weight: '.1', max_daily_added_weight: '.3', max_order_absolute: '1000',
    max_daily_added_absolute: '3000', max_industry_weight: null, max_entries_per_day: null }
  const trendHtml = render({ language: 'en', policy: trend })
  assert.ok(trendHtml.includes('Active US equities'))
  assert.ok(trendHtml.includes('10%') && trendHtml.includes('30%') && trendHtml.includes('$1,000'))
  assert.ok(!trendHtml.includes('190') && !trendHtml.includes('Industry exposure'))
  const feasibility = { status: 'no_affordable_candidates', candidate_count: 6, estimated_affordable_count: 0,
    max_buy_budget: '5.22', minimum_candidate_close: '68.25' }
  const affordabilityHtml = render({ language: 'zh', policy, config, entryFeasibility: feasibility })
  for (const text of ['6 个候选均不足以买入 1 整股', '$5.22', '$68.25', '开启后等待满足资金条件的新信号', '实际以执行时报价为准']) assert.ok(affordabilityHtml.includes(text), text)
  assert.ok(!affordabilityHtml.includes('disabled'), 'Affordability estimates must not prevent activation')
  const someAffordableHtml = render({ language: 'zh', policy, config,
    entryFeasibility: { ...feasibility, status: 'some_affordable_candidates', estimated_affordable_count: 2 } })
  assert.ok(someAffordableHtml.includes('6 个候选中有 2 个'))
  const emptyHtml = render({ language: 'zh', policy, config, entryFeasibility: { status: 'no_candidates', candidate_count: 0 } })
  assert.ok(emptyHtml.includes('暂无买入候选'))
  const prepared = { config }
  assert.deepEqual(managedPolicyBlockers(prepared, { config }, policy), [])
  assert.deepEqual(managedPolicyBlockers(null, { config }), ['UI_POLICY_UNCONFIRMED'])
  assert.deepEqual(managedPolicyBlockers({ config: { ...config, scope_mode: 'manual' } }, { config }), ['UI_POLICY_UNCONFIRMED'])
  assert.deepEqual(managedPolicyBlockers(prepared, { config: { ...config, policy_hash: 'new-policy' } }), ['UI_POLICY_CHANGED'])
  assert.deepEqual(managedPolicyBlockers(prepared, { config }, { ...policy, policy_hash: 'updated-code' }), ['UI_POLICY_CHANGED'])
  assert.deepEqual(managedPolicyBlockers(prepared, { config: { ...config, universe_hash: 'changed-universe' } }), ['UI_POLICY_CHANGED'])
  console.log('Managed policy SSR and confirmation guard checks passed')
} finally {
  await rm(temp, { recursive: true, force: true })
}
