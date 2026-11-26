<script setup lang="ts">
import BuiltinSources from './settings/BuiltinSources.vue'
import KeysCard from './settings/KeysCard.vue'
import CustomSources from './settings/CustomSources.vue'
</script>

<template>
  <div class="page">
    <div class="page-head"><h1>Settings</h1></div>
    <BuiltinSources />
    <KeysCard />
    <CustomSources />
  </div>
</template>
