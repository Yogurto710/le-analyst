Page({
  data: {
    lang: 'zh',
    content: ''
  },

  onLoad() {
    const r = wx.getStorageSync('lastReport') || {}
    this.zh_md = r.zh_md || ''
    this.en_md = r.en_md || r.zh_md || ''
    this.render()
  },

  setZh() { this.setData({ lang: 'zh' }); this.render() },
  setEn() { this.setData({ lang: 'en' }); this.render() },

  render() {
    const md = this.data.lang === 'zh' ? this.zh_md : this.en_md
    this.setData({ content: md || '(no content)' })
  }
})
