<script setup>
defineProps({ index:String, title:String, meta:String })
</script>
<template><section class="list-section"><header class="section-heading"><div><span>{{index}}</span><h2>{{title}}</h2></div><slot name="action"><small>{{meta}}</small></slot></header><div class="record-list"><slot/></div></section></template>
