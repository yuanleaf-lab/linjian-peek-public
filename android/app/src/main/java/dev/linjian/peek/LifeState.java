package dev.linjian.peek;

import android.app.AppOpsManager;
import android.app.usage.UsageEvents;
import android.app.usage.UsageStatsManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.ApplicationInfo;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.os.BatteryManager;
import android.os.Build;
import android.os.PowerManager;
import android.os.Process;
import org.json.JSONObject;
import java.text.SimpleDateFormat;
import java.util.Calendar;
import java.util.Date;
import java.util.Locale;
import java.util.TimeZone;

/** Low-privacy device state. It intentionally contains no page, node, location or sensor data. */
public final class LifeState {
    private LifeState() { }

    public static JSONObject collect(Context ctx) {
        JSONObject state = new JSONObject();
        try {
            long now = System.currentTimeMillis();
            Intent battery = ctx.registerReceiver((BroadcastReceiver) null, new IntentFilter(Intent.ACTION_BATTERY_CHANGED));
            int batteryPercent = -1; boolean charging = false; String chargingType = "unknown", batteryStatus = "unknown";
            if (battery != null) {
                int level = battery.getIntExtra(BatteryManager.EXTRA_LEVEL, -1), scale = battery.getIntExtra(BatteryManager.EXTRA_SCALE, -1);
                if (level >= 0 && scale > 0) batteryPercent = Math.round(level * 100f / scale);
                int status = battery.getIntExtra(BatteryManager.EXTRA_STATUS, -1);
                charging = status == BatteryManager.BATTERY_STATUS_CHARGING || status == BatteryManager.BATTERY_STATUS_FULL;
                chargingType = pluggedToString(battery.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0)); batteryStatus = batteryStatusToString(status);
            }
            PowerManager pm = (PowerManager) ctx.getSystemService(Context.POWER_SERVICE);
            boolean screenOn = pm != null && (Build.VERSION.SDK_INT >= 20 ? pm.isInteractive() : pm.isScreenOn());
            boolean usageReady = hasUsagePermission(ctx);
            UsageSnapshot usage = usageReady ? readUsage(ctx, now) : new UsageSnapshot();
            CurrentApp current = usageReady ? currentApp(ctx, now) : new CurrentApp();
            state.put("device_id", AppPrefs.device(ctx)); state.put("privacy_mode", AppPrefs.privacyMode(ctx)); state.put("life_state_version", AppPrefs.APP_VERSION_NAME);
            state.put("updated_at_ms", now); state.put("local_date", format(now, "yyyy-MM-dd")); state.put("local_time", format(now, "HH:mm")); state.put("timezone", TimeZone.getDefault().getID());
            state.put("battery_percent", batteryPercent); state.put("battery_status", batteryStatus); state.put("charging", charging); state.put("charging_type", chargingType);
            state.put("screen_on", screenOn); state.put("network_type", networkType(ctx)); state.put("usage_permission_ready", usageReady);
            state.put("current_package", current.packageName); state.put("current_app", current.appName); state.put("current_app_authorized", usageReady);
            state.put("screen_time_today_minutes", usage.screenTimeMinutes); state.put("unlock_count_today", usage.unlockCount);
            state.put("summary", summary(batteryPercent, charging, current.appName, usageReady));
            // Compatibility marker only. A real screen text is never created or sent in low mode.
            state.put("screen_text", ""); state.put("now_state", NowState.collect(ctx));
        } catch (Exception e) { try { state.put("error", ScreenshotService.shortMsg(e)); } catch (Exception ignored) { } }
        return state;
    }

    /** Poller entrypoint: only a package/app-label event, never an accessibility event or page detail. */
    public static void recordForegroundFromUsage(Context ctx) {
        if (!hasUsagePermission(ctx)) return;
        CurrentApp app = currentApp(ctx, System.currentTimeMillis());
        if (app.packageName.isEmpty()) return;
        ActivityEventStore.recordForegroundChange(ctx, app.packageName);
        // Low privacy mode uses the same unified usage poll for AppGate. It never
        // falls back to accessibility events or ScreenshotService.currentPackage().
        if (AppPrefs.isLowPrivacy(ctx)) AppGate.onForegroundPackage(ctx, app.packageName);
    }
    public static boolean hasUsagePermission(Context ctx) { try { AppOpsManager appOps = (AppOpsManager) ctx.getSystemService(Context.APP_OPS_SERVICE); return appOps != null && appOps.checkOpNoThrow(AppOpsManager.OPSTR_GET_USAGE_STATS, Process.myUid(), ctx.getPackageName()) == AppOpsManager.MODE_ALLOWED; } catch (Exception ignored) { return false; } }
    public static String appLabelPublic(Context ctx, String pkg) { return appLabel(ctx, pkg); }

    private static CurrentApp currentApp(Context ctx, long now) {
        CurrentApp result = new CurrentApp();
        try {
            UsageStatsManager usm = (UsageStatsManager) ctx.getSystemService(Context.USAGE_STATS_SERVICE); if (usm == null) return result;
            UsageEvents events = usm.queryEvents(Math.max(0L, now - 5 * 60_000L), now); UsageEvents.Event event = new UsageEvents.Event(); long latest = 0L;
            while (events != null && events.hasNextEvent()) { events.getNextEvent(event); if (!isForeground(event.getEventType()) || event.getPackageName() == null || event.getTimeStamp() < latest) continue; latest = event.getTimeStamp(); result.packageName = event.getPackageName(); }
            result.appName = appLabel(ctx, result.packageName);
        } catch (Exception ignored) { }
        return result;
    }
    private static UsageSnapshot readUsage(Context ctx, long now) {
        UsageSnapshot summary = new UsageSnapshot();
        try {
            UsageStatsManager usm = (UsageStatsManager) ctx.getSystemService(Context.USAGE_STATS_SERVICE); if (usm == null) return summary;
            Calendar c = Calendar.getInstance(); c.set(Calendar.HOUR_OF_DAY, 0); c.set(Calendar.MINUTE, 0); c.set(Calendar.SECOND, 0); c.set(Calendar.MILLISECOND, 0); long start = c.getTimeInMillis(), openAt = 0L;
            UsageEvents events = usm.queryEvents(start, now); UsageEvents.Event event = new UsageEvents.Event();
            while (events != null && events.hasNextEvent()) {
                events.getNextEvent(event); int type = event.getEventType(); long at = Math.max(start, Math.min(now, event.getTimeStamp()));
                if ((Build.VERSION.SDK_INT >= 28 && (type == UsageEvents.Event.KEYGUARD_HIDDEN || type == UsageEvents.Event.SCREEN_INTERACTIVE)) || (Build.VERSION.SDK_INT < 28 && type == UsageEvents.Event.USER_INTERACTION)) summary.unlockCount++;
                if (isForeground(type)) openAt = at; else if (isBackground(type) && openAt > 0 && at > openAt) { summary.screenTimeMs += at - openAt; openAt = 0L; }
            }
            if (openAt > 0 && now > openAt) summary.screenTimeMs += now - openAt;
            summary.screenTimeMinutes = (int) Math.round(summary.screenTimeMs / 60000.0);
        } catch (Exception ignored) { }
        return summary;
    }
    private static boolean isForeground(int type) { return type == UsageEvents.Event.MOVE_TO_FOREGROUND || (Build.VERSION.SDK_INT >= 29 && type == UsageEvents.Event.ACTIVITY_RESUMED); }
    private static boolean isBackground(int type) { return type == UsageEvents.Event.MOVE_TO_BACKGROUND || (Build.VERSION.SDK_INT >= 29 && (type == UsageEvents.Event.ACTIVITY_PAUSED || type == UsageEvents.Event.ACTIVITY_STOPPED)); }
    private static String appLabel(Context ctx, String pkg) { try { if (pkg == null || pkg.trim().isEmpty()) return ""; ApplicationInfo info = ctx.getPackageManager().getApplicationInfo(pkg, 0); CharSequence label = ctx.getPackageManager().getApplicationLabel(info); return label == null ? pkg : label.toString(); } catch (Exception ignored) { return pkg == null ? "" : pkg; } }
    private static String networkType(Context ctx) { try { ConnectivityManager cm = (ConnectivityManager) ctx.getSystemService(Context.CONNECTIVITY_SERVICE); if (cm == null) return "unknown"; if (Build.VERSION.SDK_INT >= 23) { Network n = cm.getActiveNetwork(); NetworkCapabilities caps = n == null ? null : cm.getNetworkCapabilities(n); if (caps == null) return "none"; if (caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) return "wifi"; if (caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR)) return "cellular"; if (caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET)) return "ethernet"; return "other"; } android.net.NetworkInfo info = cm.getActiveNetworkInfo(); return info != null && info.isConnected() ? info.getTypeName().toLowerCase(Locale.US) : "none"; } catch (Exception ignored) { return "unknown"; } }
    private static String pluggedToString(int v) { if (v == BatteryManager.BATTERY_PLUGGED_USB) return "usb"; if (v == BatteryManager.BATTERY_PLUGGED_AC) return "ac"; if (Build.VERSION.SDK_INT >= 17 && v == BatteryManager.BATTERY_PLUGGED_WIRELESS) return "wireless"; return "none"; }
    private static String batteryStatusToString(int v) { if (v == BatteryManager.BATTERY_STATUS_CHARGING) return "charging"; if (v == BatteryManager.BATTERY_STATUS_DISCHARGING) return "discharging"; if (v == BatteryManager.BATTERY_STATUS_FULL) return "full"; if (v == BatteryManager.BATTERY_STATUS_NOT_CHARGING) return "not_charging"; return "unknown"; }
    private static String summary(int battery, boolean charging, String app, boolean usageReady) { return "低权限状态：" + (app.isEmpty() ? "当前 App 未识别" : "当前在 " + app) + (battery < 0 ? "" : "；电量 " + battery + "%" + (charging ? "，充电中" : "")) + (usageReady ? "。" : "；使用情况权限未开启。"); }
    private static String format(long at, String pattern) { return new SimpleDateFormat(pattern, Locale.CHINA).format(new Date(at)); }
    public static String pretty(Context ctx) { JSONObject s = collect(ctx); return "隐私模式：低权限\n当前：" + s.optString("current_app", "未授权") + "\n电量：" + s.optInt("battery_percent", -1) + "%\n网络：" + s.optString("network_type", "unknown") + "\n屏幕时间：" + s.optInt("screen_time_today_minutes", 0) + " 分钟 · 解锁 " + s.optInt("unlock_count_today", 0) + " 次\n不读取页面内容、聊天、节点、截图、定位或传感器原始数据。"; }
    private static final class CurrentApp { String packageName = ""; String appName = ""; }
    private static final class UsageSnapshot { int screenTimeMinutes; int unlockCount; long screenTimeMs; }
}
