package dev.linjian.peek;

import android.content.Context;
import org.json.JSONObject;

/** Compatibility state for the old "now" card. Low privacy never starts sensors or location. */
public final class NowState {
    private static boolean logged;
    private NowState() { }
    public static synchronized void start(Context ctx) {
        if (ctx != null && !logged) { logged = true; DebugState.appendAndLog(ctx, "低权限模式：未启动定位或传感器采集"); }
    }
    public static JSONObject collect(Context ctx) {
        JSONObject out = new JSONObject();
        try {
            out.put("privacy_mode", AppPrefs.privacyMode(ctx));
            out.put("available", false);
            out.put("location", new JSONObject().put("permission_granted", false).put("feature_enabled", false).put("unavailable", true));
            out.put("posture", new JSONObject().put("available", false).put("unavailable", true));
            out.put("environment", new JSONObject().put("available", false).put("unavailable", true));
            out.put("summary", "低权限模式未读取定位或传感器。");
        } catch (Exception ignored) { }
        return out;
    }
    public static boolean hasLocationPermission(Context ctx) { return false; }
    public static String pretty(Context ctx) { return "隐私模式：低权限\n定位、加速度、光线和距离传感器均未读取。天气继续使用你手动填写的城市。"; }
}
