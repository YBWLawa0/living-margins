<script setup>
import { computed, inject } from 'vue'
const props = defineProps({ active: String, admin: Boolean })
const injectedAdmin = inject('isAdmin', computed(() => false))
const showAdmin = computed(() => props.admin || injectedAdmin.value)
defineEmits(['navigate'])
</script>
<template><nav class="bottom-nav" :class="{ 'nav-admin': showAdmin }" :data-active="active" :data-count="showAdmin ? 4 : 3" aria-label="主导航"><span class="nav-slider" aria-hidden="true"></span><button :class="{active:active==='home'}" @click="$emit('navigate','home')">阅读</button><button :class="{active:active==='reading'}" @click="$emit('navigate','reading')">取景</button><button v-if="showAdmin" :class="{active:active==='enroll'}" @click="$emit('navigate','enroll')">录入</button><button :class="{active:active==='profile'}" @click="$emit('navigate','profile')">我的</button></nav></template>
