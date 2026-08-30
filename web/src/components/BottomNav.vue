<script setup>
defineProps({ active: String })
defineEmits(['navigate'])
</script>
<template><nav class="bottom-nav" :data-active="active" aria-label="主导航"><span class="nav-slider" aria-hidden="true"></span><button :class="{active:active==='home'}" @click="$emit('navigate','home')">阅读</button><button :class="{active:active==='reading'}" @click="$emit('navigate','reading')">取景</button><button :class="{active:active==='profile'}" @click="$emit('navigate','profile')">我的</button></nav></template>
