<script setup lang="ts">
const props = defineProps<{ title: string }>()
</script>

<template>
  <section class="k8s-placeholder">
    <h2>{{ props.title }}</h2>
    <p>该页面已纳入 K8s 管理一期菜单骨架，功能会按 k8s规划1.txt 的六期计划逐期落地。</p>
  </section>
</template>

