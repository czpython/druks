import { beforeEach, vi } from 'vitest'

beforeEach(() => {
  // jsdom has no DataTransfer. File inputs in tests receive File[] through
  // fireEvent.change, so the replacement selection uses the same shape.
  vi.stubGlobal(
    'DataTransfer',
    class {
      files: File[] = []
      items = { add: (file: File) => this.files.push(file) }
    },
  )
  Object.defineProperties(HTMLDialogElement.prototype, {
    showModal: {
      configurable: true,
      value(this: HTMLDialogElement) {
        this.setAttribute('open', '')
      },
    },
    close: {
      configurable: true,
      value(this: HTMLDialogElement) {
        this.removeAttribute('open')
        this.dispatchEvent(new Event('close'))
      },
    },
  })
})
