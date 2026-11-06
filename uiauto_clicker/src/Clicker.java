import com.android.uiautomator.testrunner.UiAutomatorTestCase;

public class Clicker extends UiAutomatorTestCase {
    public void testClick() throws Exception {
        String action = getParams().getString("action");
        if ("swipe".equals(action)) {
            int startX = Integer.parseInt(getParams().getString("start_x"));
            int startY = Integer.parseInt(getParams().getString("start_y"));
            int endX = Integer.parseInt(getParams().getString("end_x"));
            int endY = Integer.parseInt(getParams().getString("end_y"));
            int steps = Integer.parseInt(getParams().getString("steps"));
            if (!getUiDevice().swipe(startX, startY, endX, endY, steps)) {
                throw new IllegalStateException("UiDevice.swipe returned false");
            }
        } else {
            int x = Integer.parseInt(getParams().getString("x"));
            int y = Integer.parseInt(getParams().getString("y"));
            if (!getUiDevice().click(x, y)) {
                throw new IllegalStateException("UiDevice.click returned false");
            }
        }
        Thread.sleep(500);
    }
}
