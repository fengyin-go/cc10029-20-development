import { expect, test } from '@playwright/test'

function parseCsv(text: string): string[][] {
  const records: string[][] = []
  let row: string[] = []
  let cell = ''
  let quoted = false
  const input = text.replace(/^﻿/, '')
  for (let index = 0; index < input.length; index++) {
    const char = input[index]
    if (quoted) {
      if (char === '"' && input[index + 1] === '"') { cell += '"'; index++ }
      else if (char === '"') { quoted = false }
      else { cell += char }
    } else if (char === '"' && cell === '') { quoted = true }
    else if (char === ',') { row.push(cell); cell = '' }
    else if (char === '\n') {
      row.push(cell.replace(/\r$/, ''))
      if (row.some((value) => value !== '')) records.push(row)
      row = []
      cell = ''
    } else { cell += char }
  }
  row.push(cell.replace(/\r$/, ''))
  if (row.some((value) => value !== '')) records.push(row)
  return records
}

test('代理池：可用代理数据集筛选、排序持久化、导出唯一与分配台账', async ({ page, request }) => {
  test.setTimeout(120_000)
  const browserErrors: string[] = []
  page.on('pageerror', (error) => browserErrors.push(error.message))
  page.on('response', (response) => {
    if (response.url().includes('/api/') && response.status() >= 400) {
      browserErrors.push(`${response.status()} ${response.url()}`)
    }
  })

  await page.goto('/proxy')
  await expect(page.getByTestId('available-panel')).toBeVisible()
  await expect(page.getByTestId('summary-batch')).not.toHaveText('—')
  await expect(page.locator('tbody tr[data-testid^="avail-row-"]').first()).toBeVisible()

  // 单一节点只出现一次，冲突记录保留检测依据。
  const nodes = await page.locator('tbody tr[data-testid^="avail-row-"]').evaluateAll((elements) =>
    elements.map((element) => element.getAttribute('data-node')))
  expect(new Set(nodes).size).toBe(nodes.length)
  const conflictRow = page.locator('tbody tr[data-conflict="true"]').first()
  if (await conflictRow.count()) {
    await expect(conflictRow.getByTestId('conflict-tag')).toBeVisible()
    await expect(conflictRow).toContainText('不一致')
  }

  // 按协议筛选。
  await page.getByTestId('filter-协议').selectOption('HTTP')
  await page.getByTestId('available-query').click()
  await expect(page.locator('tbody tr[data-testid^="avail-row-"]')).toHaveCount(1)
  await page.getByTestId('available-reset').click()
  await expect(page.locator('tbody tr[data-testid^="avail-row-"]').first()).toBeVisible()

  // 延迟筛选。
  await page.getByTestId('filter-延迟').fill('250')
  await page.getByTestId('available-query').click()
  const latencies = await page.locator('tbody tr[data-testid^="avail-row-"] td:nth-child(8)').allInnerTexts()
  for (const value of latencies) {
    expect(Number(value.replace('ms', ''))).toBeLessThanOrEqual(250)
  }
  await page.getByTestId('available-reset').click()
  await expect(page.locator('tbody tr[data-testid^="avail-row-"]').first()).toBeVisible()

  // 排序后刷新仍保持顺序。
  const before = await page.locator('tbody tr[data-testid^="avail-row-"]').evaluateAll((elements) =>
    elements.map((element) => element.getAttribute('data-node')))
  if (before.length >= 2) {
    await page.locator('tbody tr[data-testid^="avail-row-"]').first().getByTestId('move-down').click()
    const after = await page.locator('tbody tr[data-testid^="avail-row-"]').evaluateAll((elements) =>
      elements.map((element) => element.getAttribute('data-node')))
    expect(after[0]).toBe(before[1])
    await page.reload()
    await expect(page.locator('tbody tr[data-testid^="avail-row-"]').first()).toBeVisible()
    const refreshed = await page.locator('tbody tr[data-testid^="avail-row-"]').evaluateAll((elements) =>
      elements.map((element) => element.getAttribute('data-node')))
    expect(refreshed).toEqual(after)
  }

  // 导出里的节点唯一。
  const downloadPromise = page.waitForEvent('download')
  await page.getByTestId('avail-export').click()
  const download = await downloadPromise
  expect(download.suggestedFilename()).toBe('proxy-available.csv')
  const path = await download.path()
  const text = await (await import('node:fs/promises')).readFile(path, 'utf-8')
  const records = parseCsv(text)
  const header = records[0]
  const nodeIndex = header.indexOf('节点编号')
  expect(nodeIndex).toBeGreaterThanOrEqual(0)
  const exported = records.slice(1).map((record) => record[nodeIndex])
  expect(new Set(exported).size).toBe(exported.length)

  // 生成分配记录只引用当前可用节点。
  const assignmentResponse = await request.post('/api/proxy/assignments', {
    data: { 任务编号: 'CRAW-0003' },
  })
  const assignmentPayload = await assignmentResponse.json()
  expect(assignmentPayload.ok).toBe(true)
  const available = await (await request.get('/api/proxy/available?size=200')).json()
  const allowed = new Set(available.items.filter((item: any) => item.可分配).map((item: any) => item.节点编号))
  for (const item of assignmentPayload.entry.items) {
    expect(allowed.has(item.节点编号)).toBe(true)
    expect(item.检测依据.length).toBeGreaterThan(0)
  }

  // 重复送交同一数据集不会新增记录。
  const collection = available.summary.集合编号
  const duplicate = await (await request.post('/api/proxy/available', { data: { 集合编号: collection } })).json()
  expect(duplicate.ok).toBe(true)
  expect(duplicate.message).toContain('已存在')
  const afterDuplicate = await (await request.get('/api/proxy/available?size=200')).json()
  expect(afterDuplicate.total).toBe(available.total)

  // 事务失败回滚，保留上一版本。
  const beforeRollback = afterDuplicate.items.map((item: any) => `${item.节点编号}:${item.排序序号}`)
  const rollback = await (await request.post('/api/proxy/available', { data: { simulate_failure: true } })).json()
  expect(rollback.ok).toBe(false)
  expect(rollback.message).toContain('回滚')
  const afterRollback = await (await request.get('/api/proxy/available?size=200')).json()
  expect(afterRollback.items.map((item: any) => `${item.节点编号}:${item.排序序号}`)).toEqual(beforeRollback)

  expect(browserErrors).toEqual([])
})
