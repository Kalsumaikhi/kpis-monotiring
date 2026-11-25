<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  CategoryScale, Chart, Legend, LinearScale, LineController, LineElement, PointElement, Tooltip,
} from 'chart.js'
import { fmtNum } from '../lib/format'

Chart.register(LineController, LineElement, PointElement, LinearScale, CategoryScale, Tooltip, Legend)

export interface ChartSeries { label: string; data: (number | null)[]; colorIndex: number }

const props = defineProps<{ labels: string[]; series: ChartSeries[]; label: string }>()

const canvas = ref<HTMLCanvasElement | null>(null)
let chart: Chart | null = null
const DASHES: number[][] = [[], [6, 4], [2, 3], [8, 3, 2, 3], [], [6, 4]]

const fmtTick = (v: number | string) => {
  const n = Number(v)
  return Math.abs(n) >= 10000 ? fmtNum(n) : String(n)
}

function css(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}

function build() {
  if (!canvas.value) return
  chart?.destroy()
  const text = css('--text')
  const muted = css('--text-muted')
  const grid = css('--border')
  const surface = css('--surface')
  const dense = props.labels.length > 40
  chart = new Chart(canvas.value, {
    type: 'line',
    data: {
      labels: props.labels,
      datasets: props.series.map((s) => {
        const color = css(`--series-${s.colorIndex + 1}`)
        return {
          label: s.label,
          data: s.data,
          borderColor: color,
          backgroundColor: color,
          borderWidth: 2,
          borderDash: DASHES[s.colorIndex] ?? [],
          tension: 0,
          spanGaps: false,
          pointRadius: dense ? 0 : 4,
          pointHoverRadius: 6,
          pointBackgroundColor: color,
          pointBorderColor: surface,
          pointBorderWidth: 2,
        }
      }),
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: { ticks: { color: muted, maxRotation: 0, autoSkip: true, maxTicksLimit: 10 }, grid: { display: false }, border: { color: grid } },
        y: { ticks: { color: muted, callback: (v) => fmtTick(v) }, grid: { color: grid }, border: { display: false } },
      },
      plugins: {
        legend: {
          display: props.series.length > 1,
          position: 'bottom',
          labels: { color: text, usePointStyle: true, boxWidth: 8, boxHeight: 8 },
        },
        tooltip: {
          backgroundColor: surface,
          titleColor: text,
          bodyColor: text,
          borderColor: grid,
          borderWidth: 1,
          padding: 10,
          boxPadding: 4,
          callbacks: {
            label: (c) => `${c.dataset.label}: ${c.parsed.y === null ? '—' : fmtTick(c.parsed.y)}`,
          },
        },
      },
    },
  })
}

// Re-render when the theme changes: the data-theme attribute or the OS preference.
let observer: MutationObserver | null = null
let mql: MediaQueryList | null = null
const rebuild = () => build()

onMounted(() => {
  build()
  observer = new MutationObserver(rebuild)
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
  mql = window.matchMedia?.('(prefers-color-scheme: dark)') ?? null
  mql?.addEventListener?.('change', rebuild)
})
onBeforeUnmount(() => {
  observer?.disconnect()
  mql?.removeEventListener?.('change', rebuild)
  chart?.destroy()
  chart = null
})
watch(() => [props.labels, props.series], build, { deep: true })
</script>

<template>
  <div class="chart-box">
    <canvas ref="canvas" role="img" :aria-label="label"></canvas>
  </div>
</template>

<style scoped>
.chart-box { position: relative; height: 320px; width: 100%; }
@media (max-width: 480px) { .chart-box { height: 260px; } }
</style>
