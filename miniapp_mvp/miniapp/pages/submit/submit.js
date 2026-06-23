const app = getApp()

// Any CJK Unified Ideograph triggers the zh default.
const CJK_RE = /[一-鿿]/

Page({
  data: {
    ticker: '',
    question: '',
    lang: 'en',           // current selection
    langAuto: true,       // true until the user clicks the lang toggle (auto-detect from question)
    reportType: 'initiate', // default to deep brief (the primary product)
    submitting: false,
    showDisclaimer: false,
    loginError: '',
    // 1 initiate (3 credits) + 2 research (1 credit) fits the 5-quota
    quotaHint: '每日额度:5 (投资简报 = 3,问题研究 = 1)'
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

  onTicker(e) { this.setData({ ticker: e.detail.value.toUpperCase() }) },

  onQuestion(e) {
    const question = e.detail.value
    const update = { question }
    // Auto-detect language from question only while the user hasn't tapped
    // the lang toggle (and only when in research mode where a question exists).
    if (this.data.langAuto && this.data.reportType === 'research') {
      update.lang = CJK_RE.test(question) ? 'zh' : 'en'
    }
    this.setData(update)
  },

  onLang(e) {
    this.setData({ lang: e.currentTarget.dataset.lang, langAuto: false })
  },

  onReportType(e) {
    const reportType = e.currentTarget.dataset.type
    this.setData({ reportType })
  },

  async submit() {
    const { ticker, question, lang, reportType } = this.data
    if (!ticker) {
      wx.showToast({ title: '请填写股票代码', icon: 'none' })
      return
    }
    if (reportType === 'research' && (!question || question.trim().length < 5)) {
      wx.showToast({ title: '问题研究需要填写问题', icon: 'none' })
      return
    }
    if (!app.globalData.token) {
      await app.login()
    }
    if (!app.globalData.token) {
      wx.showToast({ title: '登录失败,请稍后重试', icon: 'none' })
      return
    }

    this.setData({ submitting: true })
    const body = { ticker, lang, report_type: reportType }
    if (reportType === 'research') {
      body.question = question
    }
    wx.request({
      url: app.globalData.apiBase + '/jobs',
      method: 'POST',
      header: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + app.globalData.token
      },
      data: body,
      success: (r) => {
        this.setData({ submitting: false })
        if (r.statusCode === 200 && r.data.job_id) {
          wx.navigateTo({ url: '/pages/status/status?jobId=' + r.data.job_id + '&reportType=' + reportType })
        } else if (r.statusCode === 429) {
          wx.showToast({
            title: (r.data && r.data.detail) || '今日额度已用尽',
            icon: 'none',
            duration: 3500
          })
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
