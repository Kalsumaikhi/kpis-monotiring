import { onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

const str = (v: unknown) => (typeof v === 'string' ? v : '')

/** A search box bound to the `q` URL query: typing updates the URL after `delay` ms and resets paging. */
export function useQuerySearch(delay = 300) {
  const route = useRoute()
  const router = useRouter()
  const text = ref(str(route.query.q))
  let timer: ReturnType<typeof setTimeout> | undefined

  watch(text, (v) => {
    clearTimeout(timer)
    timer = setTimeout(() => {
      if (v === str(route.query.q)) return
      const q: Record<string, string> = {}
      for (const [k, val] of Object.entries(route.query)) if (typeof val === 'string' && k !== 'q' && k !== 'page') q[k] = val
      if (v) q.q = v
      router.replace({ query: q })
    }, delay)
  })
  // Moving to another step/page cancels a pending search so a stale q is not injected there.
  watch(() => route.path, () => {
    clearTimeout(timer)
    text.value = str(route.query.q)
  })
  watch(() => route.query.q, (v) => { text.value = str(v) })
  onBeforeUnmount(() => clearTimeout(timer))
  return text
}
