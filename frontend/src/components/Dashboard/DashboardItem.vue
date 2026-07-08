<template>
  <div class="h-full w-full">
    <div
      v-if="item.type == 'number_chart'"
      class="flex h-full w-full rounded shadow overflow-hidden cursor-pointer"
    >
      <Tooltip :text="__(item.data?.isEmpty ? item.data.emptyReason : item.data?.tooltip)">
        <NumberChart
          v-if="item.data"
          :key="index"
          class="!items-start"
          :config="item.data"
        >
          <template v-if="item.data.isEmpty" #subtitle>
            <div
              class="flex flex-1 items-center gap-0.5 flex-shrink-0 truncate text-[24px] text-ink-gray-4 font-semibold leading-10"
            >
              —
            </div>
          </template>
          <template v-if="item.data.isEmpty" #delta>
            <div />
          </template>
        </NumberChart>
      </Tooltip>
    </div>
    <div
      v-else-if="item.type == 'spacer'"
      class="rounded bg-surface-base h-full overflow-hidden text-ink-gray-5 flex items-center justify-center"
      :class="editing ? 'border border-dashed border-outline-gray-2' : ''"
    >
      {{ editing ? __('Spacer') : '' }}
    </div>
    <div
      v-else-if="item.type == 'axis_chart'"
      class="h-full w-full rounded-md bg-surface-base shadow"
    >
      <AxisChart v-if="item.data" :config="item.data" />
    </div>
    <div
      v-else-if="item.type == 'donut_chart'"
      class="h-full w-full rounded-md bg-surface-base shadow overflow-hidden"
    >
      <DonutChart v-if="item.data" :config="item.data" />
    </div>
  </div>
</template>
<script setup>
import { AxisChart, DonutChart, NumberChart, Tooltip } from 'frappe-ui'

defineProps({
  index: { type: Number, required: true },
  item: { type: Object, required: true },
  editing: { type: Boolean, default: false },
})
</script>
