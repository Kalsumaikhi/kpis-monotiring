<script setup lang="ts">
import { inject, nextTick, onBeforeUnmount, onMounted, watch } from 'vue'
import { routeLocationKey } from 'vue-router'
import BuiltinSources from './settings/BuiltinSources.vue'
import KeysCard from './settings/KeysCard.vue'
import CustomSources from './settings/CustomSources.vue'

// /settings#logins lands on the Keys card (the router leaves hash scrolling to us). The card
// grows when its rows arrive, so scroll again once it has loaded; also on a later #logins.
const atLogins = () => window.location.hash === '#logins'
let afterLoad = atLogins()

async function scrollToLogins() {
  await nextTick()
  document.getElementById('logins')?.scrollIntoView?.({ block: 'start' })
}

function onKeysLoaded() {
  if (!afterLoad) return
  afterLoad = false
  void scrollToLogins()
}

function onHashChange() {
  if (atLogins()) void scrollToLogins()
}

const route = inject(routeLocationKey, null) // null without a router (unit tests)
if (route) watch(() => route.hash, (h) => { if (h === '#logins') void scrollToLogins() })

onMounted(() => {
  window.addEventListener('hashchange', onHashChange)
  if (atLogins()) void scrollToLogins()
})
onBeforeUnmount(() => window.removeEventListener('hashchange', onHashChange))
</script>

<template>
  <div class="page">
    <div class="page-head"><h1>Settings</h1></div>
    <BuiltinSources />
    <KeysCard @loaded="onKeysLoaded" />
    <CustomSources />
  </div>
</template>
