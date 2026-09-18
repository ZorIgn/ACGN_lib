using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

[assembly: System.Reflection.AssemblyTitle("ACGLib")]
[assembly: System.Reflection.AssemblyProduct("ACGLib 私人书架")]
[assembly: System.Reflection.AssemblyVersion("0.4.0.0")]

internal static class Launcher
{
    internal static readonly string Root = AppDomain.CurrentDomain.BaseDirectory;

    [STAThread]
    private static void Main(string[] args)
    {
        string id;
        using (var sha = SHA256.Create())
            id = BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(Root.ToLowerInvariant()))).Replace("-", "");
        bool created;
        using (var mutex = new Mutex(true, "Local\\ACGLib-" + id, out created))
        using (var open = new EventWaitHandle(false, EventResetMode.AutoReset, "Local\\ACGLib-open-" + id))
        using (var stop = new EventWaitHandle(false, EventResetMode.AutoReset, "Local\\ACGLib-stop-" + id))
        {
            bool stopping = Array.IndexOf(args, "--stop") >= 0;
            if (!created)
            {
                if (stopping) stop.Set();
                else if (Array.IndexOf(args, "--background") < 0) open.Set();
                if (stopping)
                {
                    try { if (mutex.WaitOne(60000)) mutex.ReleaseMutex(); else Environment.ExitCode = 1; }
                    catch (AbandonedMutexException) { mutex.ReleaseMutex(); }
                }
                return;
            }
            try
            {
                if (stopping) { RunScript("stop.ps1"); return; }
                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                SynchronizationContext.SetSynchronizationContext(new WindowsFormsSynchronizationContext());
                Application.Run(new LibraryTray(open, stop, Array.IndexOf(args, "--background") < 0));
            }
            catch (Exception error)
            {
                Environment.ExitCode = 1;
                MessageBox.Show(error.Message, "ACGLib", MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
            finally { mutex.ReleaseMutex(); }
        }
    }

    internal static void RunScript(string name)
    {
        var info = new ProcessStartInfo("powershell.exe", "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File \"" + Path.Combine(Root, name) + "\"");
        info.WorkingDirectory = Root;
        info.UseShellExecute = false;
        info.CreateNoWindow = true;
        using (var process = Process.Start(info))
        {
            process.WaitForExit();
            if (process.ExitCode != 0) throw new InvalidOperationException("启动或停止失败。请确认 8000 端口未被其他程序占用，并检查应用目录中的 src\\db\\runtime-error.log。");
        }
    }
}

internal sealed class LibraryTray : ApplicationContext
{
    private readonly NotifyIcon tray;
    private readonly System.Windows.Forms.Timer timer;
    private readonly ToolStripMenuItem openItem;
    private bool starting = true;
    private bool closing;
    private bool stopping;
    private bool openAfterStart;

    internal LibraryTray(EventWaitHandle open, EventWaitHandle stop, bool showBrowser)
    {
        openAfterStart = showBrowser;
        var menu = new ContextMenuStrip();
        openItem = new ToolStripMenuItem("打开书架", null, (sender, e) => OpenLibrary());
        menu.Items.Add(openItem);
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("退出 ACGLib", null, (sender, e) => StopLibrary());
        tray = new NotifyIcon {
            Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath),
            Text = "ACGLib · 正在启动", ContextMenuStrip = menu, Visible = true
        };
        tray.DoubleClick += (sender, e) => OpenLibrary();
        timer = new System.Windows.Forms.Timer { Interval = 250 };
        timer.Tick += (sender, e) => {
            if (stop.WaitOne(0)) StopLibrary();
            if (open.WaitOne(0)) OpenLibrary();
        };
        timer.Start();
        StartLibrary();
    }

    private async void StartLibrary()
    {
        try
        {
            await Task.Run(() => Launcher.RunScript("start.ps1"));
            starting = false;
            tray.Text = "ACGLib · 书架运行中";
            if (closing) StopLibrary();
            else if (openAfterStart) OpenLibrary();
        }
        catch (Exception error)
        {
            starting = false;
            MessageBox.Show(error.Message, "ACGLib", MessageBoxButtons.OK, MessageBoxIcon.Error);
            ExitThread();
        }
    }

    private void OpenLibrary()
    {
        if (closing) return;
        if (starting) { openAfterStart = true; return; }
        Process.Start(new ProcessStartInfo("http://127.0.0.1:8000/library/") { UseShellExecute = true });
    }

    private async void StopLibrary()
    {
        closing = true;
        tray.Text = "ACGLib · 正在退出";
        openItem.Enabled = false;
        if (starting || stopping) return;
        stopping = true;
        try { await Task.Run(() => Launcher.RunScript("stop.ps1")); ExitThread(); }
        catch (Exception error)
        {
            closing = false;
            stopping = false;
            openItem.Enabled = true;
            tray.Text = "ACGLib · 退出失败，请重试";
            MessageBox.Show(error.Message, "ACGLib", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }

    protected override void ExitThreadCore()
    {
        timer.Stop();
        timer.Dispose();
        tray.Visible = false;
        tray.Dispose();
        base.ExitThreadCore();
    }
}
