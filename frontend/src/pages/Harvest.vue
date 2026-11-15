<script setup>
import { computed, onMounted, ref } from 'vue'
import { getJson, postJson } from '../api'

const domains = ref([])
const domainId = ref(1)
const state = ref(null)
const jobs = ref([])
const err = ref('')
const notice = ref('')
const busy = ref(false)

const pending = computed(() => state.value?.pending_files || [])
const watermark = computed(() => state.value?.watermark || [])

async function refresh() {
  const id = domainId.value
  const [w, j] = await Promise.all([
    getJson(`/api/harvest/watermark?domain_id=${id}`),
    getJson(`/api/harvest/jobs?domain_id=${id}`),
  ])
  state.value = w
  jobs.value = j
}

onMounted(async () => {
  try {
    domains.value = await getJson('/api/domains')
    if (domains.value.length) domainId.value = domains.value[0].id
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
  }
})

async function run() {
  busy.value = true
  err.value = ''
  notice.value = ''
  try {
    const job = await postJson('/api/harvest/run', { domain_id: domainId.value })
    notice.value = `采集任务 ${job.id}：${job.status}，${job.message}`
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
    await refresh().catch(() => {})
  } finally {
    busy.value = false
  }
}

async function switchDomain() {
  err.value = ''
  notice.value = ''
  try {
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
  }
}
</script>
<template>
  <div>
    <h2>采集位点</h2>
    <p class="lead">
      监视本地夹具目录中按业务日命名的切片文件。位点是「已成功完整摄入的切片集合」，
      每次采集只处理位点之外的切片；整文件写入成功才推进位点，失败切片不收录并保留文件名与原因。
    </p>
    <div class="toolbar">
      <label>
        主题域
        <select v-model.number="domainId" @change="switchDomain">
          <option v-for="d in domains" :key="d.id" :value="d.id">{{ d.name }}</option>
        </select>
      </label>
      <button :disabled="busy" @click="run">触发采集</button>
      <span class="muted" v-if="state">目录：{{ state.fixture_dir }}</span>
    </div>
    <p v-if="err" class="err">{{ err }}</p>
    <p v-if="notice" class="ok">{{ notice }}</p>
    <p v-if="state?.fixture_dir_error" class="err">{{ state.fixture_dir_error }}</p>

    <div class="stat-strip" v-if="state">
      <div class="item"><div class="label">位点切片</div><div class="value">{{ watermark.length }}</div></div>
      <div class="item"><div class="label">待采切片</div><div class="value">{{ pending.length }}</div></div>
      <div class="item"><div class="label">目录切片</div><div class="value">{{ state.available_files.length }}</div></div>
      <div class="item"><div class="label">最近采集</div><div class="value">{{ jobs[0]?.status || '—' }}</div></div>
    </div>

    <div class="panel">
      <h3>位点集合（已完整摄入）</h3>
      <table v-if="watermark.length">
        <thead>
          <tr><th>切片文件</th><th>业务日</th><th>行数</th><th>字节</th><th>任务</th><th>收录时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="w in watermark" :key="w.file_name">
            <td>{{ w.file_name }}</td>
            <td>{{ w.biz_day || '—' }}</td>
            <td>{{ w.row_count }}</td>
            <td>{{ w.size_bytes }}</td>
            <td>{{ w.harvest_job_id }}</td>
            <td>{{ w.ingested_at }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="muted">位点为空，触发一次采集即可按整文件收录切片。</p>
    </div>

    <div class="panel">
      <h3>待采切片（位点之外）</h3>
      <table v-if="pending.length">
        <thead><tr><th>切片文件</th><th>业务日</th><th>字节</th></tr></thead>
        <tbody>
          <tr v-for="p in pending" :key="p.file_name">
            <td>{{ p.file_name }}</td>
            <td>{{ p.biz_day || '—' }}</td>
            <td>{{ p.size_bytes }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="muted">没有待采切片。</p>
    </div>

    <div class="panel">
      <h3>采集任务记录</h3>
      <table v-if="jobs.length">
        <thead>
          <tr>
            <th>编号</th><th>状态</th><th>说明</th><th>位点前</th><th>位点后</th>
            <th>尝试</th><th>成功</th><th>失败</th><th>新增行数</th><th>时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="j in jobs" :key="j.id">
            <td>{{ j.id }}</td>
            <td :class="j.status === '成功' ? 'ok' : 'warn'">{{ j.status }}</td>
            <td>{{ j.message }}</td>
            <td>{{ j.watermark_before.length }}</td>
            <td>{{ j.watermark_after.length }}</td>
            <td>{{ j.attempted_files.join('、') || '—' }}</td>
            <td>{{ j.success_files.join('、') || '—' }}</td>
            <td>
              <template v-if="j.failed_files.length">
                <div v-for="f in j.failed_files" :key="f.file">
                  {{ f.file }}（{{ f.reason }}）
                </div>
              </template>
              <template v-else>—</template>
            </td>
            <td>{{ j.new_count }}</td>
            <td>{{ j.created_at }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="muted">还没有采集任务，点击「触发采集」开始。</p>
    </div>
  </div>
</template>
