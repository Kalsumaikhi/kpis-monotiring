<script setup lang="ts">
import { nextTick, onMounted } from 'vue'
import BuiltinSources from './settings/BuiltinSources.vue'
import KeysCard from './settings/KeysCard.vue'
import CustomSources from './settings/CustomSources.vue'

// /settings#logins lands on the Keys card (the router leaves hash scrolling to us).
onMounted(async () => {
  if (window.location.hash !== '#logins') return
  await nextTick()
  document.getElementById('logins')?.scrollIntoView?.({ block: 'start' })
})
</script>

<template>
  <div class="page">
    <div class="page-head"><h1>Settings</h1></div>
    <BuiltinSources />
    <KeysCard />
    <CustomSources />
  </div>
</template>
