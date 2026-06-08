// Global app — wx.login on launch, exchange code for a session token,
// stash it in globalData so every page can pick it up.
App({
  globalData: {
    // Replace with your real backend URL. Must be HTTPS in production;
    // in the WeChat DevTools you can toggle "不校验合法域名" while iterating.
    apiBase: 'https://YOUR_BACKEND_DOMAIN',
    token: null,
    loginError: null
  },

  onLaunch() {
    this.login()
  },

  // Exposed so a page can retry if it lands before the initial login completes.
  login() {
    const that = this
    return new Promise((resolve) => {
      wx.login({
        success(res) {
          if (!res.code) {
            that.globalData.loginError = 'wx.login returned no code'
            return resolve(null)
          }
          wx.request({
            url: that.globalData.apiBase + '/auth/wx-login',
            method: 'POST',
            header: { 'Content-Type': 'application/json' },
            data: { code: res.code },
            success(r) {
              if (r.statusCode === 200 && r.data && r.data.token) {
                that.globalData.token = r.data.token
                that.globalData.loginError = null
                resolve(r.data.token)
              } else {
                that.globalData.loginError =
                  '登录失败:' + (r.data && r.data.detail ? r.data.detail : r.statusCode)
                resolve(null)
              }
            },
            fail(err) {
              that.globalData.loginError = '网络错误:' + (err.errMsg || 'unknown')
              resolve(null)
            }
          })
        },
        fail(err) {
          that.globalData.loginError = 'wx.login 失败:' + (err.errMsg || 'unknown')
          resolve(null)
        }
      })
    })
  }
})
