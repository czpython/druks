const queues = new Map<string, Promise<void>>()

function enqueue<T>(accountId: string, write: () => Promise<T>): Promise<T> {
  const previous = queues.get(accountId)
  let request: Promise<T>
  try {
    request = previous ? previous.catch(() => undefined).then(write) : write()
  } catch (error) {
    request = Promise.reject(error)
  }
  const settled = request.then(
    () => undefined,
    () => undefined,
  )
  queues.set(accountId, settled)
  void settled.then(() => {
    if (queues.get(accountId) === settled) queues.delete(accountId)
  })
  return request
}

/** Serialize settings PATCHes per account. */
export function queuePersonalSettingsWrite<T>(accountId: string, write: () => Promise<T>): Promise<T> {
  return enqueue(accountId, write)
}

/** Recheck a pending retry when it reaches the queue, not only when it is scheduled. */
export function queueCurrentPersonalSettingsWrite<T>(
  accountId: string,
  isCurrent: () => boolean,
  write: () => Promise<T>,
): Promise<T | null> {
  return enqueue(accountId, () => (isCurrent() ? write() : Promise.resolve(null)))
}
