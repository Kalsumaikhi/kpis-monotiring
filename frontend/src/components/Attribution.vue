<script setup lang="ts">
defineProps<{ text: string | null | undefined }>()
</script>

<template>
  <p v-if="text" class="attribution">{{ text }}</p>
</template>

<style scoped>
.attribution { font-size: 0.8125rem; color: var(--text-muted); }
</style>
