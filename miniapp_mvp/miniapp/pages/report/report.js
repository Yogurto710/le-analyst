Page({
  data: { content: '' },

  onLoad() {
    const r = wx.getStorageSync('lastReport') || {}
    // Prefer the Chinese translation; fall back to English if --translate
    // didn't run for some reason (canonical English is never lost).
    this.setData({ content: r.zh_md || r.en_md || '(no content)' })
  }
})
