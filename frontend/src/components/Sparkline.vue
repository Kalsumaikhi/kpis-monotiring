<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(defineProps<{ values: number[]; width?: number; height?: number }>(), { width: 96, height: 24 })

const label = computed(() => `changes per month: ${props.values.join(', ')}`)
const points = computed(() => {
  const v = props.values
  if (!v.length) return []
  const max = Math.max(...v, 1)
  const pad = 2
  const w = props.width - pad * 2
  const h = props.height - pad * 2
  return v.map((n, i) => ({
    x: pad + (v.length === 1 ? w / 2 : (i * w) / (v.length - 1)),
    y: pad + h - (n / max) * h,
  }))
})
const path = computed(() => points.value.map((p, i) => `${i ? 'L' : 'M'}${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(' '))
const last = computed(() => points.value[points.value.length - 1])
</script>

<template>
  <svg class="spark" :width="width" :height="height" :viewBox="`0 0 ${width} ${height}`" role="img" :aria-label="label">
    <path :d="path" fill="none" stroke="var(--accent)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />
    <circle v-if="last" :cx="last.x" :cy="last.y" r="2.5" fill="var(--accent)" stroke="var(--surface)" stroke-width="2" />
  </svg>
</template>

<style scoped>
.spark { display: block; overflow: visible; }
</style>
