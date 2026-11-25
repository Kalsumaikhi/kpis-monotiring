<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ page: number; pageSize: number; total: number }>()
const emit = defineEmits<{ 'update:page': [page: number] }>()

const pages = computed(() => Math.max(1, Math.ceil(props.total / props.pageSize)))
const from = computed(() => (props.total === 0 ? 0 : (props.page - 1) * props.pageSize + 1))
const to = computed(() => Math.min(props.total, props.page * props.pageSize))
</script>

<template>
  <nav class="pager" aria-label="Pagination">
    <span class="muted">{{ from }}–{{ to }} of {{ total }}</span>
    <span class="btns">
      <button class="btn secondary" type="button" :disabled="page <= 1" @click="emit('update:page', page - 1)">Previous</button>
      <span class="muted">Page {{ page }} of {{ pages }}</span>
      <button class="btn secondary" type="button" :disabled="page >= pages" @click="emit('update:page', page + 1)">Next</button>
    </span>
  </nav>
</template>

<style scoped>
.pager { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px 16px; font-size: 0.875rem; }
.btns { display: inline-flex; align-items: center; gap: 8px; flex-wrap: wrap; }
</style>
