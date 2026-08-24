import { describe, expect, it } from 'vitest'
import fs from 'node:fs'
import path from 'node:path'


const source = fs.readFileSync(
  path.resolve(process.cwd(), 'src/components/ChatPanel.vue'),
  'utf8',
)


describe('runtime capability feature gates', () => {
  it('does not render model selection when the runtime lacks it', () => {
    expect(source).toContain('v-if="runtimeCapabilities.model_selection"')
    expect(source).toContain('Model managed by runtime')
  })

  it('does not send unsupported model or resume controls', () => {
    expect(source).toContain('model: runtimeCapabilities.value.model_selection')
    expect(source).toContain('resumeSessionIdLocal.value && runtimeCapabilities.value.session_load')
  })

  it('loads the negotiated feature snapshot', () => {
    expect(source).toContain('/runtime/capabilities')
    expect(source).toContain('response.data.capabilities')
  })
})
