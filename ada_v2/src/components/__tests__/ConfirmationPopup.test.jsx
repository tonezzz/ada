import React from 'react'
import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import ConfirmationPopup from '../ConfirmationPopup'


describe('ConfirmationPopup', () => {
  it('renders nothing when request is null', () => {
    const { container } = render(
      <ConfirmationPopup request={null} onConfirm={() => {}} onDeny={() => {}} />
    )

    expect(container.firstChild).toBeNull()
  })

  it('renders tool name and args and calls callbacks', async () => {
    const user = userEvent.setup()
    const onConfirm = vi.fn()
    const onDeny = vi.fn()

    render(
      <ConfirmationPopup
        request={{ tool: 'generate_cad', args: { prompt: 'cube' } }}
        onConfirm={onConfirm}
        onDeny={onDeny}
      />
    )

    expect(screen.getByText('generate_cad')).toBeInTheDocument()
    expect(screen.getByText(/cube/)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /deny request/i }))
    expect(onDeny).toHaveBeenCalledTimes(1)

    await user.click(screen.getByRole('button', { name: /authorize execution/i }))
    expect(onConfirm).toHaveBeenCalledTimes(1)
  })
})
