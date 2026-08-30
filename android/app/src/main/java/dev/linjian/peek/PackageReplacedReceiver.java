package dev.linjian.peek;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.util.Log;

public class PackageReplacedReceiver extends BroadcastReceiver {
    @Override public void onReceive(Context context, Intent intent) {
        if (context == null || intent == null || !Intent.ACTION_MY_PACKAGE_REPLACED.equals(intent.getAction())) return;
        String server = AppPrefs.server(context);
        String token = AppPrefs.token(context);
        if (server.isEmpty() || token.isEmpty()) {
            DebugState.append(context, "安装更新后未恢复服务：服务器地址或 Token 为空");
            Log.i("LinjianPeek", "Package replaced; service not restored because configuration is incomplete");
            return;
        }
        Intent service = new Intent(context, CompanionService.class);
        service.putExtra("server_url", server);
        service.putExtra("token", token);
        if (Build.VERSION.SDK_INT >= 26) context.startForegroundService(service);
        else context.startService(service);
        DebugState.append(context, "安装更新后已尝试恢复前台服务");
        Log.i("LinjianPeek", "Package replaced; requested CompanionService restore");
    }
}
