import { describe, expect, it, vi } from 'vitest'

import type { SlashRunCtx } from '../types.js'
import { coreCommands } from './core.js'

describe('tokens preference persistence', () => {
  it.each(['always', 'off'])('reports persistence only after %s is saved', async arg => {
    let resolve!: () => void
    const pending = new Promise<void>(ok => { resolve = ok })
    const rpc = vi.fn(() => pending)
    const sys = vi.fn()
    const ctx = { gateway: { rpc }, transcript: { sys } } as unknown as SlashRunCtx
    const command = coreCommands.find(item => item.name === 'tokens')!
    command.run(arg, ctx, 'tokens')
    expect(rpc).toHaveBeenCalledWith('config.set', {
      key: 'display.show_message_tokens', value: arg === 'always' ? 'on' : 'off'
    })
    expect(sys).not.toHaveBeenCalled()
    resolve()
    await pending
    await Promise.resolve()
    expect(sys).toHaveBeenCalledWith(expect.stringContaining('(saved)'))

    sys.mockClear()
    rpc.mockImplementationOnce(() => Promise.reject(new Error('cannot write')))
    command.run(arg, ctx, 'tokens')
    await Promise.resolve()
    await Promise.resolve()
    expect(sys).toHaveBeenCalledWith(expect.stringContaining('could not save preference'))
  })
})
