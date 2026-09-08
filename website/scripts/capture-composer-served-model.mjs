/**
 * Before/after of the composer model chip on a session that inherits its model.
 *
 * Drives website/capture/composer-served-model.html, which mounts the REAL
 * ChatInput twice and labels each chip through the REAL displayModel() on one
 * slot fixture. Every frame asserts the chip text before writing, so a frame
 * cannot document the wrong label:
 *   before  the chip reads "auto"
 *   after   the chip reads the served model the session was moved onto, marked `· default`
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6832 --strictPort   # in another shell
 *   node scripts/capture-composer-served-model.mjs http://127.0.0.1:6832 ../temp-screenshots/composer-served-model
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6832'
const OUT = process.argv[3] || '../temp-screenshots/composer-served-model'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
let failed = false

for (const theme of ['dark', 'light']) {
  const page = await browser.newPage({ viewport: { width: 760, height: 420 }, deviceScaleFactor: 2 })
  // Gateway-free: answer every REAL API call the mounted ChatInput makes.
  await page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
    const path = new URL(route.request().url()).pathname
    const isList = /commands|skills|agents|sessions|files|history|models/.test(path)
    return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
  await page.goto(`${BASE}/capture/composer-served-model.html?theme=${theme}`)
  await page.waitForSelector('[data-capture-root]')
  // The chip is the shelf button whose whole text is the model name (no test id on
  // the chip yet, and the title carries a translated prefix).
  const chip = (episode) => page.locator(`[data-episode="${episode}"] button`).filter({ hasText: /^(auto|gpt-5\.6-sol · default)$/ })
  await chip('before').waitFor()
  await chip('after').waitFor()
  const before = (await chip('before').innerText()).trim()
  const after = (await chip('after').innerText()).trim()
  const ok = before === 'auto' && after === 'gpt-5.6-sol · default'
  console.log(`${theme}: ${ok ? 'OK' : 'MISMATCH'} before="${before}" after="${after}"`)
  if (!ok) failed = true
  else await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/composer-served-model-${theme}.png` })
  await page.close()
}

await browser.close()
process.exit(failed ? 1 : 0)
