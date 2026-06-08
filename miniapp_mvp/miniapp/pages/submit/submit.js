const app = getApp()

Page({
  data: {
    ticker: '',
    question: '',
    submitting: false,
    showDisclaimer: false,
    loginError: '',
    quotaHint: '每个账号每日 5 次'
  },

  onLoad() {
    if (!wx.getStorageSync('sawDisclaimer')) {
      this.setData({ showDisclaimer: true })
    }
  },

  onShow() {
    this.setData({ loginError: app.globalData.loginError || '' })
  },

  acceptDisclaimer() {
    wx.setStorageSync('sawDisclaimer', true)
    this.setData({ showDisclaimer: false })
  },

  onTicker(e)   { this.setData({ ticker: e.detail.value.toUpperCase() }) },
  onQuestion(e) { this.setData({ question: e.detail.value }) },

  async submit() {
    const { ticker, question } = this.data
    if (!ticker || !question) {
      wx.showToast({ title: '请填写代码和问题', icon: 'none' })
      return
    }
    // Token might not be ready if the user is fast — try once more.
    if (!app.globalData.token) {
      await app.login()
    }
    if (!app.globalData.token) {
      wx.showToast({ title: '登录失败,请稍后重试', icon: 'none' })
      return
    }

    this.setData({ submitting: true })
    wx.request({
      url: app.globalData.apiBase + '/jobs',
      method: 'POST',
      header: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + app.globalData.token
      },
      data: { ticker, question },
      success: (r) => {
        this.setData({ submitting: false })
        if (r.statusCode === 200 && r.data.job_id) {
          wx.navigateTo({ url: '/pages/status/status?jobId=' + r.data.job_id })
        } else if (r.statusCode === 429) {
          wx.showToast({ title: '今日额度已用尽', icon: 'none' })
        } else {
          const detail = (r.data && r.data.detail) || ('错误 ' + r.statusCode)
          wx.showToast({ title: '提交失败:' + detail, icon: 'none' })
        }
      },
      fail: () => {
        this.setData({ submitting: false })
        wx.showToast({ title: '网络错误', icon: 'none' })
      }
    })
  }
})
