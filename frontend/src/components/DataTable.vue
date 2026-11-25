<script setup lang="ts" generic="Row extends Record<string, any>">
export interface Column {
  key: string
  label: string
  align?: 'left' | 'right' | 'center'
}
defineProps<{ columns: Column[]; rows: Row[]; loading?: boolean; empty?: string; rowKey?: string }>()
const emit = defineEmits<{ rowClick: [row: Row] }>()
</script>

<template>
  <div class="table-wrap" :aria-busy="loading ? 'true' : 'false'">
    <table>
      <thead>
        <tr>
          <th v-for="c in columns" :key="c.key" scope="col" :style="{ textAlign: c.align ?? 'left' }">{{ c.label }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-if="loading && !rows.length">
          <td :colspan="columns.length" class="state muted">Loading…</td>
        </tr>
        <tr v-else-if="!rows.length">
          <td :colspan="columns.length" class="state muted">{{ empty ?? 'Nothing to show yet.' }}</td>
        </tr>
        <template v-else>
          <tr v-for="(row, i) in rows" :key="rowKey ? row[rowKey] : i" :class="{ dim: loading }" @click="emit('rowClick', row)">
            <td v-for="c in columns" :key="c.key" :style="{ textAlign: c.align ?? 'left' }">
              <slot :name="`cell-${c.key}`" :row="row" :value="row[c.key]">{{ row[c.key] }}</slot>
            </td>
          </tr>
        </template>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.table-wrap { overflow-x: auto; max-width: 100%; border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); }
table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
th, td { padding: 9px 12px; border-bottom: 1px solid var(--border); vertical-align: middle; }
th { position: sticky; top: 0; background: var(--surface-2); font-weight: 600; color: var(--text-muted); font-size: 0.8125rem; white-space: nowrap; }
tbody tr:last-child td { border-bottom: 0; }
tbody tr:hover td { background: var(--surface-2); }
.state { text-align: center; padding: 28px 12px; }
.dim { opacity: 0.6; }
</style>
