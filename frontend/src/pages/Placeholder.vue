<script setup lang="ts">
import EmptyState from '../components/EmptyState.vue'
defineProps<{ title: string }>()
</script>

<template>
  <section class="page">
    <div class="page-head"><h1>{{ title }}</h1></div>
    <div class="card"><EmptyState title="Coming soon" /></div>
  </section>
</template>
