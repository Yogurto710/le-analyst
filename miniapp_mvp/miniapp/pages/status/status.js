const app = getApp()

const LABELS = {
  pending: '排队中',
  running: '研究中',
  done:    '已完成',
  failed:  '失败'
}

Page({
  data: {
    status: 'pending',
    elapsed: 0,
    statusLabel: '排队中',
    error: '',
    reportType: 'research',  // set from query param; affects expected wait time
    waitHint: ''
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
    this.poll()
    this.timer = setInterval(() => this.poll(), 4000)
  },

  onUnload() {
    if (this.timer) clearInterval(this.timer)
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
