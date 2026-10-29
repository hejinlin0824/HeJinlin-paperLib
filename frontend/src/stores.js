import { reactive } from 'vue'

export const store = reactive({
  toasts: [],
  _tid: 0,
})

export function toast(msg, type = 'info') {
  const id = ++store._tid
  store.toasts.push({ id, msg, type })
  setTimeout(() => {
    const i = store.toasts.findIndex(t => t.id === id)
    if (i >= 0) store.toasts.splice(i, 1)
  }, 3600)
}
