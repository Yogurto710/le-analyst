const app = getApp()

// Try to require the lottie-miniprogram runtime. If 构建 npm hasn't been
// run yet, this throws and we fall back to the CSS-animated unicode ♞
// in status.wxml — see miniapp/libs/README.md for install steps.
let lottie = null
let mascotData = null
try {
  lottie = require('lottie-miniprogram')
  mascotData = require('../../assets/lottie/robot-3d.js')
} catch (e) {
  console.warn('[status] lottie-miniprogram not available — using CSS fallback')
}

const LABELS = {
  pending: '排队中',
  running: '研究中',
  done:    '已完成',
  failed:  '失败'
}

// Live phase messages. Backend (`webapp._classify_stderr_line`) parses
// analyst.py's stderr markers into these enum values, and the mini-app
// shows what the agent is doing right now instead of a static "研究中".
const PHASE_MESSAGES = {
  prefetch:   '正在加载基础数据',
  gather:     '正在检索资料',
  compute:    '正在计算估值倍数',
  synthesize: '正在撰写报告',
  review:     '正在审核数据一致性',
  revise:     '正在修订报告',
  save:       '正在保存'
}

Page({
  data: {
    status: 'pending',
    elapsed: 0,
    statusLabel: '排队中',
    error: '',
    reportType: 'research',  // set from query param; affects expected wait time
    waitHint: '',
    lottieReady: false,      // canvas branch in wxml; otherwise unicode fallback shows
    phaseMsg: '',            // human-readable phase, live-updated from /jobs/{id}
    toolCount: 0             // running tool-call count, shown as a small progress hint
  },

  onLoad(query) {
    this.jobId = query.jobId
    const reportType = query.reportType || 'research'
    const waitHint = reportType === 'initiate'
      ? '投资简报通常需要 8-12 分钟,请保持本页打开。'
      : '问题研究通常需要 2-3 分钟,请保持本页打开。'
    this.setData({ reportType, waitHint })
    if (!this.jobId) {
      wx.showToast({ title: '缺少 job id', icon: 'none' })
      return
    }
    this.initMascot()
    this.poll()
    this.timer = setInterval(() => this.poll(), 4000)
  },

  // Lottie canvas init. The wxml flips to the canvas branch only after
  // setData({ lottieReady: true }) succeeds, so failure here keeps the
  // CSS fallback visible — never blank.
  initMascot() {
    if (!lottie || !mascotData) return
    // setData first so the wx:if branch mounts the <canvas> we then query.
    this.setData({ lottieReady: true }, () => {
      wx.createSelectorQuery()
        .in(this)
        .select('#lottie-knight')
        .fields({ node: true, size: true })
        .exec((res) => {
          if (!res || !res[0] || !res[0].node) {
            this.setData({ lottieReady: false })
            return
          }
          const canvas = res[0].node
          const ctx = canvas.getContext('2d')
          const dpr = (wx.getWindowInfo ? wx.getWindowInfo() : wx.getSystemInfoSync()).pixelRatio
          canvas.width  = res[0].width  * dpr
          canvas.height = res[0].height * dpr
          ctx.scale(dpr, dpr)
          try {
            lottie.setup(canvas)
            this.anim = lottie.loadAnimation({
              loop: true,
              autoplay: true,
              animationData: mascotData,
              rendererSettings: { context: ctx, clearCanvas: true }
            })
          } catch (e) {
            console.warn('[status] lottie init failed, falling back', e)
            this.setData({ lottieReady: false })
          }
        })
    })
  },

  onUnload() {
    if (this.timer) clearInterval(this.timer)
    if (this.anim) { try { this.anim.destroy() } catch (e) {} this.anim = null }
  },

  back() {
    wx.navigateBack()
  },

  poll() {
    if (!app.globalData.token) return
    wx.request({
      url: app.globalData.apiBase + '/jobs/' + this.jobId,
      header: { 'Authorization': 'Bearer ' + app.globalData.token },
      success: (r) => {
        if (r.statusCode !== 200) return
        const d = r.data || {}
        this.setData({
          status: d.status,
          elapsed: Math.round(d.elapsed_s || 0),
          statusLabel: LABELS[d.status] || d.status,
          phaseMsg: PHASE_MESSAGES[d.phase] || '',
          toolCount: d.tool_count || 0,
          error: d.error || ''
        })
        if (d.status === 'done') {
          clearInterval(this.timer)
          wx.setStorageSync('lastReport', {
            lang: d.lang,
            report_type: d.report_type,
            md: d.md,
            html: d.html
          })
          wx.redirectTo({ url: '/pages/report/report' })
        } else if (d.status === 'failed') {
          clearInterval(this.timer)
        }
      }
    })
  }
})
