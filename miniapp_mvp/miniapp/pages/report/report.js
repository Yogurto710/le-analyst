Page({
  data: { html: '' },

  onLoad() {
    const r = wx.getStorageSync('lastReport') || {}
    // Stash raw markdown for the copy button (backend already stripped
    // the YAML frontmatter, so this matches what's rendered on screen).
    this.md = r.md || ''
    const html =
      r.html ||
      (r.md ? '<pre>' + r.md + '</pre>' : '') ||
      '<p>(no content)</p>'
    this.setData({ html })
  },

  copyBrief() {
    const text = this.md
    if (!text) {
      wx.showToast({ title: '没有可复制的内容', icon: 'none' })
      return
    }
    wx.setClipboardData({
      data: text,
      success: () => {
        wx.showToast({ title: '已复制全文', icon: 'success' })
      },
      fail: (err) => {
        wx.showToast({
          title: '复制失败:' + (err.errMsg || 'unknown'),
          icon: 'none'
        })
      }
    })
  }
})
