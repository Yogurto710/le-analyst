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
    error: ''
  },

  onLoad(query) {
    this.jobId = query.jobId
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
            zh_md: d.zh_md,
            zh_html: d.zh_html,
            en_md: d.en_md
          })
          wx.redirectTo({ url: '/pages/report/report' })
        } else if (d.status === 'failed') {
          clearInterval(this.timer)
        }
      }
    })
  }
})
