import React from 'react'
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { act, render, screen } from '@testing-library/react'

import AuthLock from '../AuthLock'


function createFakeSocket() {
  const handlers = new Map()

  return {
    on: vi.fn((event, cb) => {
      handlers.set(event, cb)
    }),
    off: vi.fn((event) => {
      handlers.delete(event)
    }),
    emitEvent: (event, payload) => {
      const cb = handlers.get(event)
      if (cb) cb(payload)
    },
  }
}


describe('AuthLock', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('shows locked state by default', () => {
    const socket = createFakeSocket()

    render(<AuthLock socket={socket} onAuthenticated={() => {}} />)

    expect(screen.getByText(/system locked/i)).toBeInTheDocument()
    expect(screen.getByText(/initializing security/i)).toBeInTheDocument()
  })

  it('calls onAuthenticated after auth_status authenticated=true', async () => {
    const socket = createFakeSocket()
    const onAuthenticated = vi.fn()

    render(<AuthLock socket={socket} onAuthenticated={onAuthenticated} />)

    await act(async () => {
      socket.emitEvent('auth_status', { authenticated: true })
    })

    // callback fires after 2 seconds
    await act(async () => {
      vi.advanceTimersByTime(2000)
    })

    expect(onAuthenticated).toHaveBeenCalledWith(true)
  })
})
