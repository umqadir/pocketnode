// The main screen: what is going on, what is running, who is paired, the code that ran,
// settings, the console and what this app is. Rendered from status_json() only.

#import "PNStoreUI.h"
#import "PNBridge.h"
#include <sys/sysctl.h>

UIViewController *PNMakeStoreRootViewController(void) {
    UINavigationController *nav = [[UINavigationController alloc] initWithRootViewController:[PNMainViewController new]];
    nav.navigationBar.prefersLargeTitles = YES;
    return nav;
}

static NSString *const kHowItWorks =
    @"pocketnode makes this iPhone a compute node for your own Mac. Install the pocketnode package on the Mac "
    @"(pip install pocketnode), pair it here once over a cable, and Python functions you run through pocketnode "
    @"on the Mac can run on this iPhone's processor. Results go back to the Mac that sent the work and nowhere "
    @"else. Everything that runs is listed under Code with its source. Turn off Accept work to stop at any time. "
    @"The packages this app can use for compiled code are fixed and listed under About; other compiled packages "
    @"run on the Mac instead.";

static NSString *PNLink(NSString *link) {
    return [link isEqualToString:@"wifi"] ? @"Wi-Fi" : @"USB";
}

static NSString *PNJoin(NSArray<NSString *> *items, NSString *last) {
    if (items.count <= 1) return items.firstObject ?: @"";
    NSArray *head = [items subarrayWithRange:NSMakeRange(0, items.count - 1)];
    return [NSString stringWithFormat:@"%@ %@ %@", [head componentsJoinedByString:@", "], last, items.lastObject];
}

static NSString *PNMachine(void) {
    NSString *sim = NSProcessInfo.processInfo.environment[@"SIMULATOR_MODEL_IDENTIFIER"];
    if (sim.length) return sim;
    char buf[64] = {0};
    size_t len = sizeof(buf) - 1;
    if (sysctlbyname("hw.machine", buf, &len, NULL, 0) != 0) return nil;
    return [NSString stringWithUTF8String:buf];
}

@implementation PNMainViewController

- (void)viewDidLoad {
    [super viewDidLoad];
    self.title = @"pocketnode";
    [[NSNotificationCenter defaultCenter] addObserver:self selector:@selector(statusChanged)
                                                 name:PNLocalRunDidChange object:nil];
}

- (NSDictionary *)status {
    return PNStatus.shared.latest;
}

- (void)statusChanged {
    self.sections = [self buildSections];
}

#pragma mark Sentences

- (NSString *)sentence:(NSDictionary *)st {
    if (![st[@"ready"] boolValue]) return @"Starting Python…";
    if (st[@"accepting"] && ![st[@"accepting"] boolValue]) return @"Paused: not accepting work";
    NSArray *sessions = PNArray(st[@"sessions"]);
    if (sessions.count) {
        NSMutableArray *parts = [NSMutableArray array];
        for (NSDictionary *s in sessions) {
            NSDictionary *d = PNDict(s);
            NSString *mac = PNString(d[@"mac"]).length ? PNString(d[@"mac"]) : @"a Mac";
            [parts addObject:[NSString stringWithFormat:@"%@ over %@", mac, PNLink(PNString(d[@"link"]))]];
        }
        return [@"Connected to " stringByAppendingString:PNJoin(parts, @"and")];
    }
    NSArray *paired = PNArray(st[@"paired"]);
    if (!paired.count) return @"Not paired with a Mac yet";
    NSMutableArray *names = [NSMutableArray array];
    for (NSDictionary *m in paired) [names addObject:PNString(PNDict(m)[@"name"]).length ? PNString(PNDict(m)[@"name"]) : @"a Mac"];
    return [@"Waiting for " stringByAppendingString:PNJoin(names, @"or")];
}

- (NSDictionary *)sessionFor:(NSString *)pairId status:(NSDictionary *)st {
    for (id s in PNArray(st[@"sessions"]))
        if ([PNString(PNDict(s)[@"pair_id"]) isEqualToString:pairId]) return s;
    return nil;
}

#pragma mark Sections

- (NSArray<PNSection *> *)buildSections {
    NSDictionary *st = [self status];
    __weak PNMainViewController *weak = self;
    NSMutableArray<PNSection *> *out = [NSMutableArray array];
    BOOL ready = [st[@"ready"] boolValue];
    NSArray *tasks = PNArray(st[@"tasks"]);

    // 1. Status
    NSMutableArray *rows = [NSMutableArray array];
    PNRow *r = [PNRow ident:@"status" kind:PNRowPlain text:[self sentence:st] detail:nil];
    [rows addObject:r];
    if (ready) {
        r = [PNRow ident:@"accepting" kind:PNRowToggle text:@"Accept work from paired Macs" detail:nil];
        r.on = st[@"accepting"] ? [st[@"accepting"] boolValue] : YES;
        r.toggled = ^(BOOL on, UISwitch *sender) { [weak setAccepting:on sender:sender]; };
        [rows addObject:r];
    }
    if (tasks.count) {
        [rows addObject:[PNRow ident:@"running" kind:PNRowPlain
                                text:[NSString stringWithFormat:@"Running %@", PNPlural(tasks.count, @"task", @"tasks")] detail:nil]];
        r = [PNRow ident:@"stop" kind:PNRowDestructive text:@"Stop running tasks" detail:nil];
        r.action = ^{ [weak confirmStop]; };
        [rows addObject:r];
    }
    [out addObject:[PNSection header:nil rows:rows footer:nil]];
    if (!ready) return out;

    // 2. Running now
    rows = [NSMutableArray array];
    for (id t in tasks) {
        NSDictionary *task = PNDict(t);
        NSString *tid = PNString(task[@"id"]);
        NSString *label = PNString(task[@"label"]).length ? PNString(task[@"label"]) : @"task";
        NSString *detail = [NSString stringWithFormat:@"From %@, running for %@",
                            PNString(task[@"mac"]).length ? PNString(task[@"mac"]) : @"a Mac",
                            PNDuration([task[@"elapsed"] doubleValue])];
        r = [PNRow ident:[@"task:" stringByAppendingString:tid] kind:PNRowSubtitle text:label detail:detail];
        r.disclosure = YES;
        r.hint = @"Shows this function's source.";
        r.action = ^{
            PNTextViewController *v = [[PNTextViewController alloc] initWithTitle:label func:@"task_source" arg:tid];
            v.emptyText = @"The source of this function isn't available.";
            [weak.navigationController pushViewController:v animated:YES];
        };
        [rows addObject:r];
    }
    if (!rows.count) [rows addObject:[PNRow ident:@"nothing" kind:PNRowNote text:@"Nothing is running." detail:nil]];
    [out addObject:[PNSection header:@"Running now" rows:rows footer:nil]];

    // 3. Waiting for approval
    NSArray *waiting = PNArray(st[@"waiting"]);
    if (waiting.count) {
        rows = [NSMutableArray array];
        for (id w in waiting) {
            NSDictionary *item = PNDict(w);
            NSString *code = PNString(item[@"code"]);
            NSString *label = PNString(item[@"label"]).length ? PNString(item[@"label"]) : @"function";
            NSString *mac = PNString(item[@"mac"]).length ? PNString(item[@"mac"]) : @"a Mac";
            NSString *where = PNString(item[@"where"]);
            r = [PNRow ident:[NSString stringWithFormat:@"wait:%@:%@", code, PNString(item[@"pair_id"])]
                        kind:PNRowSubtitle text:label detail:[@"From " stringByAppendingString:mac]];
            r.disclosure = YES;
            r.hint = @"Shows the code, with Allow and Don't Allow.";
            r.action = ^{
                PNTextViewController *v = [[PNTextViewController alloc] initWithTitle:label func:@"code_source" arg:code];
                v.heading = where.length ? [NSString stringWithFormat:@"From %@, %@. It runs only if you allow it.", mac, where]
                                         : [NSString stringWithFormat:@"From %@. It runs only if you allow it.", mac];
                __weak PNTextViewController *wv = v;
                v.decide = ^(BOOL allow) {
                    PNApprove(code, allow, ^{ [wv.navigationController popViewControllerAnimated:YES]; });
                };
                [weak.navigationController pushViewController:v animated:YES];
            };
            [rows addObject:r];
        }
        [out addObject:[PNSection header:@"Waiting for approval" rows:rows footer:nil]];
    }

    // 4. Paired Macs
    rows = [NSMutableArray array];
    for (id m in PNArray(st[@"paired"])) {
        NSDictionary *mac = PNDict(m);
        NSString *pid = PNString(mac[@"pair_id"]);
        NSString *name = PNString(mac[@"name"]).length ? PNString(mac[@"name"]) : @"Mac";
        NSDictionary *sess = [self sessionFor:pid status:st];
        NSString *detail;
        if (sess) detail = [NSString stringWithFormat:@"Connected over %@", PNLink(PNString(sess[@"link"]))];
        else if (PNDateText(mac[@"last_seen"])) detail = [@"Last connected " stringByAppendingString:PNDateText(mac[@"last_seen"])];
        else detail = @"Not connected yet";
        r = [PNRow ident:[@"mac:" stringByAppendingString:pid] kind:PNRowSubtitle text:name detail:detail];
        r.disclosure = YES;
        r.action = ^{
            [weak.navigationController pushViewController:[[PNMacViewController alloc] initWithPairId:pid] animated:YES];
        };
        r.deleteTitle = @"Forget";
        r.deleted = ^{ PNConfirmForget(weak, mac, nil); };
        [rows addObject:r];
    }
    r = [PNRow ident:@"pair" kind:PNRowButton text:@"Pair a Mac…" detail:nil];
    r.action = ^{ [weak.navigationController pushViewController:[PNPairingViewController new] animated:YES]; };
    [rows addObject:r];
    [out addObject:[PNSection header:@"Paired Macs" rows:rows footer:nil]];

    // 5. Code (round 2 of the host API: each part shows only when status has it)
    rows = [NSMutableArray array];
    NSString *codeFooter = nil;
    if (st[@"code_count"]) {
        r = [PNRow ident:@"codelist" kind:PNRowValue text:@"Code that has run" detail:PNString(st[@"code_count"])];
        r.disclosure = YES;
        r.action = ^{ [weak.navigationController pushViewController:[PNCodeListViewController new] animated:YES]; };
        [rows addObject:r];
    }
    if (st[@"fetched_count"]) {
        r = [PNRow ident:@"fetched" kind:PNRowValue text:@"Python files from your Mac" detail:PNString(st[@"fetched_count"])];
        r.disclosure = YES;
        r.action = ^{ [weak.navigationController pushViewController:[PNFetchedViewController new] animated:YES]; };
        [rows addObject:r];
    }
    if (st[@"review"]) {
        r = [PNRow ident:@"review" kind:PNRowToggle text:@"Ask before running new code" detail:nil];
        r.on = [st[@"review"] boolValue];
        r.toggled = ^(BOOL on, UISwitch *sender) {
            [PNBridge call:@"set_review" arg:on ? @"1" : @"0" done:^(NSString *x) { [PNStatus.shared refreshNow]; }];
        };
        [rows addObject:r];
        codeFooter = @"When this is on, a function your Mac sends for the first time waits here until you allow it. "
                     @"Code from installed packages isn't asked about.";
    }
    if (rows.count) [out addObject:[PNSection header:@"Code" rows:rows footer:codeFooter]];

    // 6. Settings
    rows = [NSMutableArray array];
    r = [PNRow ident:@"awake" kind:PNRowToggle text:@"Keep screen on while connected" detail:nil];
    r.on = [st[@"keep_awake"] boolValue];
    r.toggled = ^(BOOL on, UISwitch *sender) {
        [PNBridge call:@"set_keep_awake" arg:on ? @"1" : @"0" done:^(NSString *x) { [PNStatus.shared refreshNow]; }];
    };
    [rows addObject:r];
    BOOL wifi = [st[@"wifi"] boolValue];
    r = [PNRow ident:@"wifi" kind:PNRowToggle text:@"Accept work over Wi-Fi" detail:nil];
    r.on = wifi;
    r.toggled = ^(BOOL on, UISwitch *sender) { [weak setWifi:on sender:sender]; };
    [rows addObject:r];
    NSString *addr = PNString(st[@"wifi_address"]);
    NSString *wifiFooter = !wifi ? @"USB works without any network access."
        : addr.length ? [NSString stringWithFormat:@"On your Mac: pn.connect(\"%@\")", addr]
                      : @"This iPhone has no Wi-Fi address right now. USB works without any network access.";
    [out addObject:[PNSection header:@"Settings" rows:rows footer:wifiFooter]];

    NSString *bg = PNString(st[@"background"]);
    NSDictionary *bgText = @{@"on": @"On", @"asking": @"Asking…", @"refused": @"Not allowed",
                             @"expired": @"Ended by iOS", @"off": @"Off"};
    r = [PNRow ident:@"background" kind:PNRowValue text:@"Keep running in the background" detail:bgText[bg] ?: @"Off"];
    r.enabled = tasks.count > 0 || PNLocalRun.shared.running;
    r.hint = @"Asks iOS to let the running work go on when you leave the app.";
    r.action = ^{ [weak keepComputing]; };
    [out addObject:[PNSection header:nil rows:@[r]
        footer:@"Available while work is running. iOS shows the progress and can end it at any time."]];

    // 7. Try it on this iPhone
    r = [PNRow ident:@"console" kind:PNRowSubtitle text:@"Python console"
              detail:@"Write and run Python on this iPhone. No Mac needed."];
    r.disclosure = YES;
    r.action = ^{ [weak.navigationController pushViewController:[PNConsoleViewController new] animated:YES]; };
    [out addObject:[PNSection header:@"Try it on this iPhone" rows:@[r] footer:nil]];

    // 8. About
    rows = [NSMutableArray array];
    UIDevice *dev = [UIDevice currentDevice];
    NSString *machine = PNMachine();
    [rows addObject:[PNRow ident:@"device" kind:PNRowValue text:@"This device"
        detail:machine.length ? [NSString stringWithFormat:@"%@ (%@)", dev.model, machine] : dev.model]];
    [rows addObject:[PNRow ident:@"ios" kind:PNRowValue text:dev.systemName detail:dev.systemVersion]];
    [rows addObject:[PNRow ident:@"mem" kind:PNRowValue text:@"Memory available" detail:PNBytes(st[@"avail_mem"])]];
    [rows addObject:[PNRow ident:@"cache" kind:PNRowValue text:@"Cache" detail:PNBytes(st[@"cache_bytes"])]];
    NSString *thermal = PNString(st[@"thermal"]);
    [rows addObject:[PNRow ident:@"thermal" kind:PNRowValue text:@"Thermal state"
                          detail:thermal.length ? thermal.capitalizedString : @"Unknown"]];
    [rows addObject:[PNRow ident:@"python" kind:PNRowValue text:@"Python" detail:PNString(st[@"python"])]];
    [rows addObject:[PNRow ident:@"version" kind:PNRowValue text:@"pocketnode" detail:PNString(st[@"version"])]];
    r = [PNRow ident:@"packages" kind:PNRowValue text:@"Packages in this app"
              detail:[NSString stringWithFormat:@"%lu", (unsigned long)PNDict(st[@"packages"]).count]];
    r.disclosure = YES;
    r.action = ^{ [weak.navigationController pushViewController:[PNPackagesViewController new] animated:YES]; };
    [rows addObject:r];
    r = [PNRow ident:@"how" kind:PNRowPlain text:@"How it works" detail:nil];
    r.disclosure = YES;
    r.action = ^{
        [weak.navigationController pushViewController:[[PNTextViewController alloc] initWithTitle:@"How it works"
                                                         text:kHowItWorks monospaced:NO] animated:YES];
    };
    [rows addObject:r];
    NSString *privacy = PNString(st[@"privacy_url"]);
    NSURL *privacyURL = privacy.length ? [NSURL URLWithString:privacy] : nil;
    if (privacyURL) {
        r = [PNRow ident:@"privacy" kind:PNRowButton text:@"Privacy Policy" detail:nil];
        r.hint = @"Opens in Safari.";
        r.action = ^{ [[UIApplication sharedApplication] openURL:privacyURL options:@{} completionHandler:nil]; };
        [rows addObject:r];
    }
    NSArray *notes = PNArray(st[@"notes"]);
    if (notes.count > 5) notes = [notes subarrayWithRange:NSMakeRange(notes.count - 5, 5)];
    NSMutableArray *noteLines = [NSMutableArray array];
    for (id n in notes) [noteLines addObject:PNString(n)];
    [out addObject:[PNSection header:@"About" rows:rows
                              footer:noteLines.count ? [noteLines componentsJoinedByString:@"\n"] : nil]];
    return out;
}

#pragma mark Actions

- (NSInteger)taskCount {
    return PNArray(self.status[@"tasks"]).count;
}

static NSString *PNStopsMessage(NSInteger n) {
    return n == 1 ? @"This stops 1 running task. Your Mac runs it itself, unless it was sent to this iPhone only."
                  : [NSString stringWithFormat:@"This stops %ld running tasks. Your Mac runs them itself, "
                                               @"unless they were sent to this iPhone only.", (long)n];
}

- (void)confirmStop {
    NSInteger n = [self taskCount];
    if (!n) return;
    UIAlertController *a = [UIAlertController alertControllerWithTitle:@"Stop running tasks?"
        message:PNStopsMessage(n) preferredStyle:UIAlertControllerStyleAlert];
    [a addAction:[UIAlertAction actionWithTitle:@"Cancel" style:UIAlertActionStyleCancel handler:nil]];
    [a addAction:[UIAlertAction actionWithTitle:@"Stop" style:UIAlertActionStyleDestructive handler:^(UIAlertAction *x) {
        [PNBridge call:@"stop_tasks" arg:nil done:^(NSString *r) { [PNStatus.shared refreshNow]; }];
    }]];
    [self presentViewController:a animated:YES completion:nil];
}

- (void)setAccepting:(BOOL)on sender:(UISwitch *)sender {
    void (^apply)(void) = ^{
        [PNBridge call:@"set_accepting" arg:on ? @"1" : @"0" done:^(NSString *r) { [PNStatus.shared refreshNow]; }];
    };
    NSInteger n = [self taskCount];
    if (on || !n) { apply(); return; }
    UIAlertController *a = [UIAlertController alertControllerWithTitle:@"Stop accepting work?"
        message:PNStopsMessage(n) preferredStyle:UIAlertControllerStyleAlert];
    [a addAction:[UIAlertAction actionWithTitle:@"Cancel" style:UIAlertActionStyleCancel handler:^(UIAlertAction *x) {
        [sender setOn:YES animated:YES];
    }]];
    [a addAction:[UIAlertAction actionWithTitle:@"Stop Accepting" style:UIAlertActionStyleDestructive
                                        handler:^(UIAlertAction *x) { apply(); }]];
    [self presentViewController:a animated:YES completion:nil];
}

- (void)setWifi:(BOOL)on sender:(UISwitch *)sender {
    [PNBridge call:@"set_wifi" arg:on ? @"1" : @"0" done:^(NSString *err) {
        [PNStatus.shared refreshNow];
        if (on && err.length) {
            UIAlertController *a = [UIAlertController alertControllerWithTitle:@"Wi-Fi couldn't be turned on"
                message:err preferredStyle:UIAlertControllerStyleAlert];
            [a addAction:[UIAlertAction actionWithTitle:@"OK" style:UIAlertActionStyleDefault handler:nil]];
            [self presentViewController:a animated:YES completion:nil];
        }
    }];
}

- (void)keepComputing {
    // Called straight from the tap: the store edition asks for background time only this way.
    id<PNStoreHost> host = (id<PNStoreHost>)[UIApplication sharedApplication].delegate;
    if ([host respondsToSelector:@selector(keepComputing)]) [host keepComputing];
    [PNStatus.shared refreshNow];
}

@end
