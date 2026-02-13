# 🔧 LINE Bot Troubleshooting Guide
# 機器人問題排除指南

## ❌ Problem: Bot Leaves Group Immediately
## 問題：機器人加入群組後立即退出

### ✅ Solutions (已修正)

#### 1. Code Updated - Join Event Handler Added
**已更新代碼 - 新增加入事件處理器**

The app.py has been updated to handle `JoinEvent`. When the bot joins a group, it will now:
- ✅ Send a welcome message
- ✅ Stay in the group
- ✅ Log the join event

**Changes made:**
```python
# Added JoinEvent to imports
from linebot.models import MessageEvent, TextMessage, TextSendMessage, JoinEvent

# Added join event handler
@handler.add(JoinEvent)
def handle_join(event):
    # Sends welcome message when bot joins
```

---

## 🔍 Additional Checks Needed

### 2. LINE Developers Console Settings
**LINE 開發者控制台設定**

Please verify these settings in your LINE Developers Console:

#### Step 1: Go to Your Channel Settings
1. Login to https://developers.line.biz/console/
2. Select your channel (RBOT)
3. Go to **Messaging API** tab

#### Step 2: Check These Settings

**✅ Use webhooks:** Must be **Enabled**
```
Messaging API > Use webhooks > Enabled
```

**✅ Allow bot to join group chats:** Must be **Enabled**
```
Messaging API > Allow bot to join group chats > Enabled
```

**✅ Auto-reply messages:** Should be **Disabled**
```
Messaging API > Auto-reply messages > Disabled
```
*(Otherwise bot sends duplicate messages)*

**✅ Greeting messages:** Optional (can enable with custom message)
```
Messaging API > Greeting messages > (Your choice)
```

**✅ Webhook URL:** Must be set and verified
```
Messaging API > Webhook URL > https://your-domain.com/webhook
Webhook status: Success (green checkmark)
```

---

### 3. Verify Webhook is Running

Your bot needs to be **deployed and accessible** from the internet before adding to groups.

#### Check if webhook is accessible:
```bash
# Your webhook should respond to LINE's verification
curl -X POST https://your-domain.com/webhook
```

#### Common deployment platforms:
- **Render** (https://render.com) - Free tier available
- **Railway** (https://railway.app) - Free tier available
- **Heroku** - Paid
- **Fly.io** - Free tier available
- **AWS/GCP** - Various pricing

---

### 4. Testing Checklist

Before adding bot to group again:

- [ ] Code updated with JoinEvent handler ✅ (Done)
- [ ] Environment variables set (.env file with credentials)
- [ ] Bot deployed to server with public URL
- [ ] Webhook URL set in LINE Console
- [ ] Webhook verified (green checkmark)
- [ ] "Allow bot to join group chats" enabled
- [ ] "Use webhooks" enabled
- [ ] Bot can receive and respond to messages in 1-on-1 chat

---

## 🧪 How to Test

### Test 1: One-on-One Chat (先測試一對一)
1. Add RBOT as a friend in LINE
2. Send message: `說明`
3. Bot should reply with help text
4. ✅ If this works, bot is functioning

### Test 2: Group Chat (再測試群組)
1. Create a test group (just you + 1 friend)
2. Add RBOT to the group
3. Bot should send welcome message
4. Send: `接龍 測試`
5. Bot should respond
6. ✅ If this works, bot is ready!

---

## 📝 Common Error Messages

### "Webhook URL verification failed"
**Cause:** Bot not deployed or URL incorrect
**Solution:** Deploy bot first, then set webhook URL

### "Invalid signature"
**Cause:** LINE_CHANNEL_SECRET incorrect
**Solution:** Double-check Channel Secret in .env

### "Unauthorized"
**Cause:** LINE_CHANNEL_ACCESS_TOKEN incorrect
**Solution:** Re-generate token in LINE Console

### Bot leaves immediately (no message)
**Cause:** Join event not handled (NOW FIXED ✅)
**Solution:** Use updated app.py

---

## 🚀 Next Steps

1. **Deploy your bot** to a platform (Render/Railway recommended for free tier)
2. **Set webhook URL** in LINE Developers Console
3. **Verify webhook** (should show green checkmark)
4. **Test in 1-on-1** chat first
5. **Add to group** and enjoy! 🎉

---

## 📞 Need More Help?

If bot still leaves groups after:
- ✅ Code updated
- ✅ Settings verified
- ✅ Webhook working

Check the logs on your deployment platform for error messages.

Common log locations:
- **Render:** Dashboard > Logs tab
- **Railway:** Project > Deployments > Logs
- **Heroku:** `heroku logs --tail`

---

**Last Updated:** 2026-02-11
**Status:** Join event handler added ✅
