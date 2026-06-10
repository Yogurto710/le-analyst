Page({
  data: { html: '' },

  onLoad() {
    const r = wx.getStorageSync('lastReport') || {}
    // Prefer the server-rendered styled HTML. Fall back to a minimal
    // wrap of the raw markdown so the page never goes fully blank if
    // the backend response was malformed.
    const html =
      r.zh_html ||
      (r.zh_md ? '<pre>' + r.zh_md + '</pre>' : '') ||
      (r.en_md ? '<pre>' + r.en_md + '</pre>' : '') ||
      '<p>(no content)</p>'
    this.setData({ html })
  }
})
