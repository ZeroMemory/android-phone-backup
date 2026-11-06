import android.content.res.AssetFileDescriptor;
import android.net.Uri;
import android.os.Binder;
import android.os.IBinder;
import android.os.Process;
import android.util.Base64;
import android.util.Base64OutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.lang.reflect.Method;

public final class ReadContactVCard {
    public static void main(String[] args) throws Exception {
        if (args.length != 1 || !args[0].startsWith("content://com.android.contacts/contacts/as_vcard/")) {
            throw new IllegalArgumentException("Expected one Contacts vCard URI");
        }
        Uri uri = Uri.parse(args[0]);
        if (uri.getPathSegments().size() != 3 || uri.getQuery() != null || uri.getFragment() != null) {
            throw new IllegalArgumentException("Expected one Contacts lookup key");
        }
        int uid = Process.myUid();
        if (uid != 2000 && uid != 0) {
            throw new IllegalStateException("Run with ADB shell");
        }
        String callingPackage = uid == 0 ? "root" : "com.android.shell";
        String authority = uri.getAuthority();
        Class<?> managerType = Class.forName("android.app.IActivityManager");
        Object manager = Class.forName("android.app.ActivityManager").getMethod("getService").invoke(null);
        IBinder token = new Binder();
        Object holder;
        try {
            holder = managerType.getMethod("getContentProviderExternal",
                    String.class, int.class, IBinder.class, String.class)
                    .invoke(manager, authority, 0, token, "vcard");
        } catch (NoSuchMethodException e) {
            holder = managerType.getMethod("getContentProviderExternal",
                    String.class, int.class, IBinder.class).invoke(manager, authority, 0, token);
        }
        if (holder == null) {
            throw new IllegalStateException("Contacts provider is unavailable");
        }
        try {
            Object provider = holder.getClass().getField("provider").get(holder);
            Class<?> providerType = Class.forName("android.content.IContentProvider");
            Class<?> signalType = Class.forName("android.os.ICancellationSignal");
            Class<?> callerType;
            Object caller;
            try {
                Class<?> builderType = Class.forName("android.content.AttributionSource$Builder");
                Object builder = builderType.getConstructor(int.class).newInstance(uid);
                builderType.getMethod("setPackageName", String.class).invoke(builder, callingPackage);
                caller = builderType.getMethod("build").invoke(builder);
                callerType = caller.getClass();
            } catch (ClassNotFoundException e) {
                caller = callingPackage;
                callerType = String.class;
            }
            // Contacts serves vCards as assets, while the built-in content reader opens files.
            Method open = providerType.getMethod("openAssetFile", callerType, Uri.class, String.class, signalType);
            AssetFileDescriptor asset = (AssetFileDescriptor) open.invoke(provider, caller, uri, "r", null);
            if (asset == null) {
                throw new IllegalStateException("Contacts returned no vCard stream");
            }
            // Base64 preserves CRLF bytes through the Windows ADB shell transport.
            try (InputStream input = asset.createInputStream();
                 OutputStream encoded = new Base64OutputStream(System.out, Base64.NO_WRAP | Base64.NO_CLOSE)) {
                byte[] buffer = new byte[8192];
                for (int count; (count = input.read(buffer)) != -1;) {
                    encoded.write(buffer, 0, count);
                }
            }
        } finally {
            managerType.getMethod("removeContentProviderExternal", String.class, IBinder.class)
                    .invoke(manager, authority, token);
        }
    }
}
