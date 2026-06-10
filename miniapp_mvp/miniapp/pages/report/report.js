Page({
  data: { html: '' },

  onLoad() {
    const r = wx.getStorageSync('lastReport') || {}
    // Stash raw markdown for the copy button (backend already stripped
    // the YAML frontmatter, so this matches what's rendered on screen).
    this.zh_md = r.zh_md || ''
    this.en_md = r.en_md || ''
    // Prefer the server-rendered styled HTML. Fall back to a minimal
    // wrap of the raw markdown so the page never goes fully blank if
    // the backend response was malformed.
    const html =
      r.zh_html ||
      (r.zh_md ? '<pre>' + r.zh_md + '</pre>' : '') ||
      (r.en_md ? '<pre>' + r.en_md + '</pre>' : '') ||
      '<p>(no content)</p>'
    this.setData({ html })
  },

  copyBrief() {
    const text = this.zh_md || this.en_md
    if (!text) {
      wx.showToast({ title: '没有可复制的内容', icon: 'none' })
      return
    }
    wx.setClipboardData({
      data: text,
      success: () => {
        // WeChat shows its own toast ("内容已复制") on setClipboardData
        // success on some versions; ours is a fallback for builds that
        // don't.
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
