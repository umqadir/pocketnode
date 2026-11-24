// Thin host for the pocketnode worker: embed CPython, run the worker on a
// big-stack thread, forward lifecycle changes, and keep computing in the
// background through a continued-processing task when the user asks.

#import "AppDelegate.h"
#import <BackgroundTasks/BackgroundTasks.h>
#import <Python/Python.h>
#import <Metal/Metal.h>
#import <objc/runtime.h>
#include <pthread.h>

#pragma mark - macOS Metal selectors

// Packages built for macOS ask the GPU questions that only exist there
// (PyTorch's Metal backend calls -isLowPower while choosing a device, and an
// unrecognized selector aborts the app). An iPhone has one GPU: it is never
// headless or removable. Whether it reports "low power" decides whether such
// code uses it at all, so that follows the app's torch_mps setting.
static BOOL pn_gpu_enabled = NO;
static BOOL pn_no(id self, SEL _cmd) { return NO; }
static BOOL pn_low_power(id self, SEL _cmd) { return !pn_gpu_enabled; }

static void pn_add_macos_metal_selectors(void) {
    id<MTLDevice> device = MTLCreateSystemDefaultDevice();
    if (!device) return;
    Class cls = object_getClass(device);
    struct { const char *name; IMP imp; } add[] = {
        {"isLowPower", (IMP)pn_low_power}, {"isHeadless", (IMP)pn_no}, {"isRemovable", (IMP)pn_no},
    };
    for (size_t i = 0; i < sizeof(add) / sizeof(add[0]); i++) {
        SEL sel = sel_registerName(add[i].name);
        if (![device respondsToSelector:sel]) class_addMethod(cls, sel, add[i].imp, "B@:");
    }
}

static NSString *const kTaskPrefix = @"compute";

@interface SceneDelegate : UIResponder <UIWindowSceneDelegate>
@property (strong, nonatomic) UIWindow *window;
@end

@interface AppDelegate ()
@property (strong) UILabel *statusLabel;
@property (strong) UIButton *keepButton;
@property (strong) NSTimer *timer;
@property (strong) BGContinuedProcessingTask *bgTask;
@property (assign) UIBackgroundTaskIdentifier graceTask;
@property (assign) BOOL pythonReady;
@property (assign) BOOL bgPending;
- (void)keepComputing;
- (void)stopComputing;
@end

// Called from Python (ctypes) on any thread: 1 asks iOS for continued
// processing, 0 gives it back.
__attribute__((visibility("default"), used))
void pocketnode_keep_computing(int on) {
    dispatch_async(dispatch_get_main_queue(), ^{
        AppDelegate *app = (AppDelegate *)[UIApplication sharedApplication].delegate;
        if (on) [app keepComputing]; else [app stopComputing];
    });
}

#pragma mark - Python

static NSString *py_call(const char *func, const char *arg) {
    // Call pocketnode_ios.<func>(arg) and return str(result). Safe from any thread.
    if (!Py_IsInitialized()) return nil;
    PyGILState_STATE g = PyGILState_Ensure();
    NSString *out = nil;
    PyObject *mod = PyImport_ImportModule("pocketnode_ios");
    if (mod) {
        PyObject *res = arg ? PyObject_CallMethod(mod, func, "s", arg) : PyObject_CallMethod(mod, func, NULL);
        if (res) {
            PyObject *s = PyObject_Str(res);
            if (s) {
                const char *c = PyUnicode_AsUTF8(s);
                if (c) out = [NSString stringWithUTF8String:c];
                Py_DECREF(s);
            }
            Py_DECREF(res);
        }
        Py_DECREF(mod);
    }
    if (PyErr_Occurred()) PyErr_Print();
    PyGILState_Release(g);
    return out;
}

static void *python_main(void *ctx) {
    NSString *resources = [[NSBundle mainBundle] resourcePath];
    NSData *settings = [NSData dataWithContentsOfFile:[resources stringByAppendingPathComponent:@"pocketnode_settings.json"]];
    if (settings) {
        NSDictionary *s = [NSJSONSerialization JSONObjectWithData:settings options:0 error:nil];
        if ([s isKindOfClass:[NSDictionary class]]) pn_gpu_enabled = [s[@"torch_mps"] boolValue];
    }
    pn_add_macos_metal_selectors();
    PyPreConfig pre;
    PyConfig config;
    PyPreConfig_InitIsolatedConfig(&pre);
    PyConfig_InitIsolatedConfig(&config);
    pre.utf8_mode = 1;
    config.buffered_stdio = 0;
    config.install_signal_handlers = 0;
    // Bytecode can't go in the signed bundle; cache it in Library/Caches.
    NSString *caches = NSSearchPathForDirectoriesInDomains(NSCachesDirectory, NSUserDomainMask, YES)[0];
    NSString *pycache = [caches stringByAppendingPathComponent:@"pycache"];
    config.write_bytecode = 1;

    PyStatus st = Py_PreInitialize(&pre);
    if (PyStatus_Exception(st)) { NSLog(@"pocketnode: Python preinit failed: %s", st.err_msg); return NULL; }
    wchar_t *w = Py_DecodeLocale([[resources stringByAppendingPathComponent:@"python"] UTF8String], NULL);
    PyConfig_SetString(&config, &config.home, w);
    PyMem_RawFree(w);
    w = Py_DecodeLocale([pycache UTF8String], NULL);
    PyConfig_SetString(&config, &config.pycache_prefix, w);
    PyMem_RawFree(w);
    st = PyConfig_Read(&config);
    if (!PyStatus_Exception(st)) st = Py_InitializeFromConfig(&config);
    PyConfig_Clear(&config);
    if (PyStatus_Exception(st)) { NSLog(@"pocketnode: Python init failed: %s", st.err_msg); return NULL; }

    NSString *boot = [NSString stringWithFormat:
        @"import site, sys\n"
        @"site.addsitedir(%@)\n"
        @"sys.path.insert(0, %@)\n",
        [NSString stringWithFormat:@"r'''%@/app_packages'''", resources],
        [NSString stringWithFormat:@"r'''%@/app'''", resources]];
    if (PyRun_SimpleString([boot UTF8String]) != 0) { NSLog(@"pocketnode: bootstrap failed"); }
    AppDelegate *app = (__bridge AppDelegate *)ctx;
    dispatch_async(dispatch_get_main_queue(), ^{ app.pythonReady = YES; });
    // Runs the worker server forever, releasing the GIL whenever it blocks.
    PyRun_SimpleString("import pocketnode_ios; pocketnode_ios.main()");
    NSLog(@"pocketnode: worker exited");
    return NULL;
}

@implementation AppDelegate

- (BOOL)application:(UIApplication *)application didFinishLaunchingWithOptions:(NSDictionary *)launchOptions {
    self.graceTask = UIBackgroundTaskInvalid;
    [UIApplication sharedApplication].idleTimerDisabled = YES;  // screen-lock suspends us otherwise

    pthread_attr_t attr;
    pthread_attr_init(&attr);
    pthread_attr_setstacksize(&attr, 16 * 1024 * 1024);
    pthread_t thread;
    pthread_create(&thread, &attr, python_main, (__bridge void *)self);
    pthread_attr_destroy(&attr);

    __weak AppDelegate *weak = self;
    self.timer = [NSTimer scheduledTimerWithTimeInterval:1.0 repeats:YES block:^(NSTimer *t) { [weak refresh]; }];
    return YES;
}

- (UISceneConfiguration *)application:(UIApplication *)application
    configurationForConnectingSceneSession:(UISceneSession *)session
                                   options:(UISceneConnectionOptions *)options {
    UISceneConfiguration *c = [[UISceneConfiguration alloc] initWithName:@"Default" sessionRole:session.role];
    c.delegateClass = [SceneDelegate class];
    return c;
}

- (UIWindow *)buildUIInScene:(UIWindowScene *)scene {
    self.window = [[UIWindow alloc] initWithWindowScene:scene];
    UIViewController *vc = [UIViewController new];
    // The screen stays on while the app is in front (locking would suspend it),
    // so keep it black: on an OLED panel black pixels are off.
    self.window.overrideUserInterfaceStyle = UIUserInterfaceStyleDark;
    vc.view.backgroundColor = [UIColor blackColor];

    UILabel *title = [UILabel new];
    title.text = @"pocketnode";
    title.font = [UIFont systemFontOfSize:28 weight:UIFontWeightSemibold];
    title.textColor = [UIColor colorWithWhite:0.55 alpha:1];

    self.statusLabel = [UILabel new];
    self.statusLabel.numberOfLines = 0;
    self.statusLabel.font = [UIFont monospacedSystemFontOfSize:14 weight:UIFontWeightRegular];
    self.statusLabel.text = @"starting Python…";
    self.statusLabel.textColor = [UIColor colorWithWhite:0.45 alpha:1];

    self.keepButton = [UIButton buttonWithType:UIButtonTypeSystem];
    [self.keepButton setTitle:@"Keep computing in the background" forState:UIControlStateNormal];
    [self.keepButton addTarget:self action:@selector(keepComputing) forControlEvents:UIControlEventTouchUpInside];

    UIStackView *stack = [[UIStackView alloc] initWithArrangedSubviews:@[title, self.statusLabel, self.keepButton]];
    stack.axis = UILayoutConstraintAxisVertical;
    stack.spacing = 16;
    stack.alignment = UIStackViewAlignmentLeading;
    stack.translatesAutoresizingMaskIntoConstraints = NO;
    [vc.view addSubview:stack];
    [NSLayoutConstraint activateConstraints:@[
        [stack.leadingAnchor constraintEqualToAnchor:vc.view.layoutMarginsGuide.leadingAnchor],
        [stack.trailingAnchor constraintEqualToAnchor:vc.view.layoutMarginsGuide.trailingAnchor],
        [stack.topAnchor constraintEqualToAnchor:vc.view.safeAreaLayoutGuide.topAnchor constant:24],
    ]];
    self.window.rootViewController = vc;
    [self.window makeKeyAndVisible];
    return self.window;
}

- (void)refresh {
    if (!self.pythonReady) return;
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_UTILITY, 0), ^{
        NSString *s = py_call("status", NULL);
        if (!s) return;
        dispatch_async(dispatch_get_main_queue(), ^{ self.statusLabel.text = s; });
        BGContinuedProcessingTask *t = self.bgTask;
        if (t) {
            // Continued-processing tasks must show progress or they're expired as stalled.
            NSString *done = py_call("tasks_done", NULL);
            t.progress.totalUnitCount = 1000000;
            t.progress.completedUnitCount = MIN(999999, done.longLongValue + 1);
            [t updateTitle:@"pocketnode" subtitle:[NSString stringWithFormat:@"%@ tasks done for your Mac", done]];
        }
    });
}

#pragma mark - Lifecycle

- (void)applicationDidEnterBackground:(UIApplication *)application {
    py_call("lifecycle", self.bgTask ? "background-continued" : "background");
    if (!self.bgTask) {
        // Up to ~30 s to tell the Mac and hand back anything that just finished.
        self.graceTask = [application beginBackgroundTaskWithName:@"pocketnode-pause" expirationHandler:^{
            [application endBackgroundTask:self.graceTask];
            self.graceTask = UIBackgroundTaskInvalid;
        }];
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 25 * NSEC_PER_SEC), dispatch_get_main_queue(), ^{
            if (self.graceTask != UIBackgroundTaskInvalid) {
                [application endBackgroundTask:self.graceTask];
                self.graceTask = UIBackgroundTaskInvalid;
            }
        });
    }
}

- (void)applicationWillEnterForeground:(UIApplication *)application {
    py_call("lifecycle", "active");
}

- (void)applicationWillTerminate:(UIApplication *)application {
    py_call("lifecycle", "terminating");
}

- (void)applicationDidReceiveMemoryWarning:(UIApplication *)application {
    py_call("memory_warning", NULL);
}

#pragma mark - Continued processing

// Ask iOS to let the work continue if the user leaves the app. The worker calls
// this when tasks arrive (and stopComputing when it has been idle), and the
// button does the same by hand. Each request needs its own identifier under the
// wildcard in Info.plist, and its handler is registered just before submitting:
// a handler registered for the wildcard itself is never matched, and submitting
// without a matching handler throws.
- (void)keepComputing {
    if (self.bgTask || self.bgPending) return;
    NSString *ident = [NSString stringWithFormat:@"%@.%@.%@", [NSBundle mainBundle].bundleIdentifier, kTaskPrefix,
                       [[NSUUID UUID].UUIDString substringToIndex:8]];
    __weak AppDelegate *weak = self;
    @try {
        BOOL ok = [[BGTaskScheduler sharedScheduler] registerForTaskWithIdentifier:ident usingQueue:nil
                                                                     launchHandler:^(BGTask *task) {
            [weak runContinued:(BGContinuedProcessingTask *)task];
        }];
        if (!ok) { py_call("continued", "refused: identifier not permitted"); return; }
        BGContinuedProcessingTaskRequest *req = [[BGContinuedProcessingTaskRequest alloc]
            initWithIdentifier:ident title:@"pocketnode" subtitle:@"Computing for your Mac"];
        req.strategy = BGContinuedProcessingTaskRequestSubmissionStrategyFail;
        NSError *err = nil;
        self.bgPending = YES;
        if (![[BGTaskScheduler sharedScheduler] submitTaskRequest:req error:&err]) {
            self.bgPending = NO;
            NSLog(@"pocketnode: continued processing refused: %@", err);
            py_call("continued", [[NSString stringWithFormat:@"refused: %@", err.localizedDescription] UTF8String]);
        }
    } @catch (NSException *e) {
        self.bgPending = NO;
        py_call("continued", [[NSString stringWithFormat:@"refused: %@", e.reason] UTF8String]);
    }
}

- (void)stopComputing {
    BGContinuedProcessingTask *t = self.bgTask;
    if (!t) return;
    self.bgTask = nil;
    [t setTaskCompletedWithSuccess:YES];
    py_call("continued", "off");
    [self.keepButton setTitle:@"Keep computing in the background" forState:UIControlStateNormal];
}

- (void)runContinued:(BGContinuedProcessingTask *)task {
    self.bgTask = task;
    self.bgPending = NO;
    py_call("continued", "on");
    __weak AppDelegate *weak = self;
    task.expirationHandler = ^{
        [task setTaskCompletedWithSuccess:NO];
        weak.bgTask = nil;
        py_call("continued", "expired");
        dispatch_async(dispatch_get_main_queue(), ^{
            if ([UIApplication sharedApplication].applicationState == UIApplicationStateBackground)
                py_call("lifecycle", "background");
            [weak.keepButton setTitle:@"Keep computing in the background" forState:UIControlStateNormal];
        });
    };
    dispatch_async(dispatch_get_main_queue(), ^{
        [weak.keepButton setTitle:@"Computing in the background ✓" forState:UIControlStateNormal];
    });
}

@end

@implementation SceneDelegate

- (void)scene:(UIScene *)scene willConnectToSession:(UISceneSession *)session options:(UISceneConnectionOptions *)options {
    AppDelegate *app = (AppDelegate *)UIApplication.sharedApplication.delegate;
    self.window = [app buildUIInScene:(UIWindowScene *)scene];
}

- (void)sceneDidEnterBackground:(UIScene *)scene {
    [(AppDelegate *)UIApplication.sharedApplication.delegate applicationDidEnterBackground:UIApplication.sharedApplication];
}

- (void)sceneWillEnterForeground:(UIScene *)scene {
    [(AppDelegate *)UIApplication.sharedApplication.delegate applicationWillEnterForeground:UIApplication.sharedApplication];
}

@end
