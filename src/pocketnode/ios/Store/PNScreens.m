// The store edition's secondary screens: text and source, pairing, one Mac, the code log,
// the files fetched from the Mac and the packages in the app.

#import "PNStoreUI.h"
#import "PNBridge.h"

#pragma mark - Text and source

@implementation PNTextViewController {
    NSString *_text, *_func, *_arg;
    BOOL _mono, _loaded;
    UITextView *_view;
}

- (instancetype)initWithTitle:(NSString *)title text:(NSString *)text monospaced:(BOOL)mono {
    self = [super initWithNibName:nil bundle:nil];
    self.title = title;
    _text = [text copy];
    _mono = mono;
    _loaded = YES;
    return self;
}

- (instancetype)initWithTitle:(NSString *)title func:(NSString *)func arg:(NSString *)arg {
    self = [super initWithNibName:nil bundle:nil];
    self.title = title;
    _func = [func copy];
    _arg = [arg copy];
    _mono = YES;
    return self;
}

- (void)viewDidLoad {
    [super viewDidLoad];
    self.navigationItem.largeTitleDisplayMode = UINavigationItemLargeTitleDisplayModeNever;
    self.view.backgroundColor = [UIColor systemBackgroundColor];
    _view = [UITextView new];
    _view.editable = NO;
    _view.selectable = YES;
    _view.adjustsFontForContentSizeCategory = YES;
    _view.backgroundColor = [UIColor systemBackgroundColor];
    _view.textContainerInset = UIEdgeInsetsMake(16, 12, 24, 12);
    _view.alwaysBounceVertical = YES;
    _view.translatesAutoresizingMaskIntoConstraints = NO;
    [self.view addSubview:_view];
    [NSLayoutConstraint activateConstraints:@[
        [_view.leadingAnchor constraintEqualToAnchor:self.view.leadingAnchor],
        [_view.trailingAnchor constraintEqualToAnchor:self.view.trailingAnchor],
        [_view.topAnchor constraintEqualToAnchor:self.view.topAnchor],
        [_view.bottomAnchor constraintEqualToAnchor:self.view.bottomAnchor],
    ]];
    if (self.decide) {
        __weak PNTextViewController *weak = self;
        UIBarButtonItem *deny = [[UIBarButtonItem alloc] initWithPrimaryAction:
            [UIAction actionWithTitle:@"Don't Allow" image:nil identifier:nil handler:^(UIAction *a) { [weak answer:NO]; }]];
        UIBarButtonItem *allow = [[UIBarButtonItem alloc] initWithPrimaryAction:
            [UIAction actionWithTitle:@"Allow" image:nil identifier:nil handler:^(UIAction *a) { [weak answer:YES]; }]];
        allow.style = UIBarButtonItemStyleProminent;
        UIBarButtonItem *flex = [UIBarButtonItem flexibleSpaceItem];
        self.toolbarItems = @[deny, flex, allow];
    }
    [self render];
    if (!_loaded) {
        [PNBridge call:_func arg:_arg done:^(NSString *result) {
            self->_loaded = YES;
            self->_text = result;
            [self render];
        }];
    }
}

- (void)viewWillAppear:(BOOL)animated {
    [super viewWillAppear:animated];
    [self.navigationController setToolbarHidden:!self.decide animated:animated];
}

- (void)viewWillDisappear:(BOOL)animated {
    [super viewWillDisappear:animated];
    if (self.decide) [self.navigationController setToolbarHidden:YES animated:animated];
}

- (void)answer:(BOOL)allow {
    void (^decide)(BOOL) = self.decide;
    self.decide = nil;  // one answer
    for (UIBarButtonItem *b in self.toolbarItems) b.enabled = NO;
    if (decide) decide(allow);
}

- (void)render {
    NSMutableAttributedString *s = [NSMutableAttributedString new];
    if (self.heading.length) {
        [s appendAttributedString:[[NSAttributedString alloc] initWithString:[self.heading stringByAppendingString:@"\n\n"]
            attributes:@{NSFontAttributeName: [UIFont preferredFontForTextStyle:UIFontTextStyleFootnote],
                         NSForegroundColorAttributeName: [UIColor secondaryLabelColor]}]];
    }
    NSString *body;
    UIColor *color = [UIColor labelColor];
    if (!_loaded) {
        body = @"Loading…";
        color = [UIColor secondaryLabelColor];
    } else if (_text.length) {
        body = _text;
    } else {
        body = self.emptyText ?: @"There is nothing to show.";
        color = [UIColor secondaryLabelColor];
    }
    UIFont *font = (_mono && _text.length) ? PNMonoFont(UIFontTextStyleFootnote) : [UIFont preferredFontForTextStyle:UIFontTextStyleBody];
    NSMutableParagraphStyle *p = [NSMutableParagraphStyle new];
    p.lineSpacing = _mono ? 1 : 4;
    [s appendAttributedString:[[NSAttributedString alloc] initWithString:body attributes:@{
        NSFontAttributeName: font, NSForegroundColorAttributeName: color, NSParagraphStyleAttributeName: p}]];
    _view.attributedText = s;
}

@end

#pragma mark - Pairing

@implementation PNPairingViewController {
    UILabel *_intro, *_code, *_timer;
    UIButton *_again;
    NSSet<NSString *> *_pairedBefore;
    NSDate *_begun;
    BOOL _expired, _done;
    UIAlertController *_ask;
}

- (void)viewDidLoad {
    [super viewDidLoad];
    self.title = @"Pair a Mac";
    self.navigationItem.largeTitleDisplayMode = UINavigationItemLargeTitleDisplayModeNever;
    self.view.backgroundColor = [UIColor systemGroupedBackgroundColor];

    _intro = [UILabel new];
    _intro.numberOfLines = 0;
    _intro.adjustsFontForContentSizeCategory = YES;
    NSMutableAttributedString *intro = [[NSMutableAttributedString alloc]
        initWithString:@"Pairing lets one Mac run Python code on this iPhone. Connect the iPhone to the Mac with a cable, then run "
        attributes:@{NSFontAttributeName: [UIFont preferredFontForTextStyle:UIFontTextStyleBody],
                     NSForegroundColorAttributeName: [UIColor labelColor]}];
    [intro appendAttributedString:[[NSAttributedString alloc] initWithString:@"pocketnode pair"
        attributes:@{NSFontAttributeName: PNMonoFont(UIFontTextStyleBody), NSForegroundColorAttributeName: [UIColor labelColor]}]];
    [intro appendAttributedString:[[NSAttributedString alloc] initWithString:@" on the Mac and type this code."
        attributes:@{NSFontAttributeName: [UIFont preferredFontForTextStyle:UIFontTextStyleBody],
                     NSForegroundColorAttributeName: [UIColor labelColor]}]];
    _intro.attributedText = intro;

    _code = [UILabel new];
    UIFont *big = [UIFont monospacedSystemFontOfSize:52 weight:UIFontWeightSemibold];
    _code.font = [[UIFontMetrics metricsForTextStyle:UIFontTextStyleLargeTitle] scaledFontForFont:big maximumPointSize:96];
    _code.adjustsFontForContentSizeCategory = YES;
    _code.adjustsFontSizeToFitWidth = YES;
    _code.minimumScaleFactor = 0.5;
    _code.textAlignment = NSTextAlignmentCenter;
    _code.text = @"------";
    _code.textColor = [UIColor tertiaryLabelColor];

    _timer = [UILabel new];
    _timer.numberOfLines = 0;
    _timer.font = [UIFont preferredFontForTextStyle:UIFontTextStyleSubheadline];
    _timer.adjustsFontForContentSizeCategory = YES;
    _timer.textColor = [UIColor secondaryLabelColor];
    _timer.textAlignment = NSTextAlignmentCenter;
    _timer.text = @" ";

    UIButtonConfiguration *cfg = [UIButtonConfiguration filledButtonConfiguration];
    cfg.title = @"Show a new code";
    _again = [UIButton buttonWithConfiguration:cfg primaryAction:nil];
    [_again addTarget:self action:@selector(begin) forControlEvents:UIControlEventTouchUpInside];
    _again.hidden = YES;

    UIStackView *stack = [[UIStackView alloc] initWithArrangedSubviews:@[_intro, _code, _timer, _again]];
    stack.axis = UILayoutConstraintAxisVertical;
    stack.spacing = 20;
    stack.alignment = UIStackViewAlignmentFill;
    [stack setCustomSpacing:32 afterView:_intro];
    [stack setCustomSpacing:8 afterView:_code];
    stack.translatesAutoresizingMaskIntoConstraints = NO;

    UIScrollView *scroll = [UIScrollView new];
    scroll.translatesAutoresizingMaskIntoConstraints = NO;
    scroll.alwaysBounceVertical = YES;
    [self.view addSubview:scroll];
    [scroll addSubview:stack];
    UILayoutGuide *content = scroll.contentLayoutGuide, *frame = scroll.frameLayoutGuide;
    [NSLayoutConstraint activateConstraints:@[
        [scroll.leadingAnchor constraintEqualToAnchor:self.view.leadingAnchor],
        [scroll.trailingAnchor constraintEqualToAnchor:self.view.trailingAnchor],
        [scroll.topAnchor constraintEqualToAnchor:self.view.topAnchor],
        [scroll.bottomAnchor constraintEqualToAnchor:self.view.bottomAnchor],
        [stack.topAnchor constraintEqualToAnchor:content.topAnchor constant:24],
        [stack.bottomAnchor constraintEqualToAnchor:content.bottomAnchor constant:-24],
        [stack.leadingAnchor constraintEqualToAnchor:frame.leadingAnchor constant:24],
        [stack.trailingAnchor constraintEqualToAnchor:frame.trailingAnchor constant:-24],
        [stack.leadingAnchor constraintEqualToAnchor:content.leadingAnchor constant:24],
        [stack.trailingAnchor constraintEqualToAnchor:content.trailingAnchor constant:-24],
    ]];

    NSMutableSet *ids = [NSMutableSet set];
    for (id m in PNArray(PNStatus.shared.latest[@"paired"])) [ids addObject:PNString(PNDict(m)[@"pair_id"])];
    _pairedBefore = ids;
    [[NSNotificationCenter defaultCenter] addObserver:self selector:@selector(statusChanged)
                                                 name:PNStatusDidChange object:nil];
    [self begin];
}

- (void)viewWillDisappear:(BOOL)animated {
    [super viewWillDisappear:animated];
    if (self.isMovingFromParentViewController) {
        [[NSNotificationCenter defaultCenter] removeObserver:self];
        if (!_done) [PNBridge call:@"pair_cancel" arg:nil done:^(NSString *r) { [PNStatus.shared refreshNow]; }];
    }
}

- (void)begin {
    _expired = NO;
    _again.hidden = YES;
    _begun = nil;
    [PNBridge call:@"pair_begin" arg:nil done:^(NSString *code) {
        if (code.length != 6) {
            self->_timer.text = @"Pairing can't start until Python is running. Try again in a moment.";
            self->_again.hidden = NO;
            return;
        }
        self->_begun = [NSDate date];
        [self showCode:code];
        [PNStatus.shared refreshNow];
    }];
}

- (void)showCode:(NSString *)code {
    _code.text = code;
    _code.textColor = [UIColor labelColor];
    NSMutableArray *digits = [NSMutableArray array];
    for (NSUInteger i = 0; i < code.length; i++) [digits addObject:[code substringWithRange:NSMakeRange(i, 1)]];
    _code.accessibilityLabel = [@"Pairing code: " stringByAppendingString:[digits componentsJoinedByString:@" "]];
}

- (void)statusChanged {
    if (_done || !_begun) return;
    NSDictionary *st = PNStatus.shared.latest;
    // Paired: a Mac we didn't have before.
    for (id m in PNArray(st[@"paired"])) {
        if (![_pairedBefore containsObject:PNString(PNDict(m)[@"pair_id"])]) {
            _done = YES;
            [self dismissAsk];
            [self.navigationController popViewControllerAnimated:YES];
            return;
        }
    }
    NSDictionary *pairing = [st[@"pairing"] isKindOfClass:[NSDictionary class]] ? st[@"pairing"] : nil;
    if (!pairing) {
        // A status read before the window opened can still say "none"; give it a moment.
        if (-_begun.timeIntervalSinceNow < 2.0 || _expired) return;
        _expired = YES;
        [self dismissAsk];
        _code.textColor = [UIColor tertiaryLabelColor];
        _timer.text = @"This code has expired.";
        _again.hidden = NO;
        UIAccessibilityPostNotification(UIAccessibilityLayoutChangedNotification, _again);
        return;
    }
    if (PNString(pairing[@"code"]).length) [self showCode:PNString(pairing[@"code"])];
    id left = pairing[@"expires_in"];
    if ([left isKindOfClass:[NSNumber class]]) {
        long s = [left longValue];
        _timer.text = [NSString stringWithFormat:@"This code works for %ld:%02ld more.", s / 60, s % 60];
    } else {
        _timer.text = @"This code stays open (debug build).";
    }
    NSDictionary *req = [pairing[@"request"] isKindOfClass:[NSDictionary class]] ? pairing[@"request"] : nil;
    if (req && !_ask) {
        [self askFor:PNString(req[@"name"]).length ? PNString(req[@"name"]) : @"a Mac"];
    } else if (!req && _ask) {
        [self dismissAsk];  // timed out or answered elsewhere
    }
}

- (void)askFor:(NSString *)name {
    UIAlertController *a = [UIAlertController
        alertControllerWithTitle:[NSString stringWithFormat:@"Allow “%@” to run Python code on this iPhone?", name]
                         message:nil preferredStyle:UIAlertControllerStyleAlert];
    __weak PNPairingViewController *weak = self;
    [a addAction:[UIAlertAction actionWithTitle:@"Don't Allow" style:UIAlertActionStyleCancel handler:^(UIAlertAction *x) {
        [weak answered:NO];
    }]];
    UIAlertAction *allow = [UIAlertAction actionWithTitle:@"Allow" style:UIAlertActionStyleDefault handler:^(UIAlertAction *x) {
        [weak answered:YES];
    }];
    [a addAction:allow];
    a.preferredAction = allow;
    _ask = a;
    [self presentViewController:a animated:YES completion:nil];
}

- (void)answered:(BOOL)allow {
    // _ask stays set until status shows no request, so the alert isn't shown twice.
    [PNBridge call:@"pair_decide" arg:allow ? @"1" : @"0" done:^(NSString *r) {
        self->_ask = nil;
        [PNStatus.shared refreshNow];
    }];
}

- (void)dismissAsk {
    if (_ask && self.presentedViewController == _ask) [_ask dismissViewControllerAnimated:YES completion:nil];
    _ask = nil;
}

@end

#pragma mark - One Mac

@implementation PNMacViewController {
    NSString *_pairId;
    BOOL _gone;
}

- (instancetype)initWithPairId:(NSString *)pairId {
    self = [super init];
    _pairId = [pairId copy];
    return self;
}

- (void)viewDidLoad {
    [super viewDidLoad];
    self.navigationItem.largeTitleDisplayMode = UINavigationItemLargeTitleDisplayModeNever;
}

- (void)statusChanged {
    NSDictionary *st = PNStatus.shared.latest;
    NSDictionary *mac = nil, *sess = nil;
    for (id m in PNArray(st[@"paired"]))
        if ([PNString(PNDict(m)[@"pair_id"]) isEqualToString:_pairId]) mac = m;
    for (id s in PNArray(st[@"sessions"]))
        if ([PNString(PNDict(s)[@"pair_id"]) isEqualToString:_pairId]) sess = s;
    if (!mac) {
        if (st && !_gone && self.navigationController.topViewController == self) {
            _gone = YES;
            [self.navigationController popViewControllerAnimated:YES];
        }
        return;
    }
    self.title = PNString(mac[@"name"]).length ? PNString(mac[@"name"]) : @"Mac";
    NSString *conn = sess ? [NSString stringWithFormat:@"Over %@", [PNString(sess[@"link"]) isEqualToString:@"wifi"] ? @"Wi-Fi" : @"USB"]
                          : @"No";
    NSMutableArray *rows = [NSMutableArray arrayWithObject:[PNRow ident:@"conn" kind:PNRowValue text:@"Connected" detail:conn]];
    if (sess && PNDateText(sess[@"since"]))
        [rows addObject:[PNRow ident:@"since" kind:PNRowValue text:@"Connected since" detail:PNDateText(sess[@"since"])]];
    [rows addObject:[PNRow ident:@"paired" kind:PNRowValue text:@"Paired" detail:PNDateText(mac[@"paired_at"]) ?: @"Unknown"]];
    [rows addObject:[PNRow ident:@"seen" kind:PNRowValue text:@"Last connected" detail:PNDateText(mac[@"last_seen"]) ?: @"Not yet"]];
    PNRow *forget = [PNRow ident:@"forget" kind:PNRowDestructive text:@"Forget This Mac" detail:nil];
    __weak PNMacViewController *weak = self;
    forget.action = ^{ PNConfirmForget(weak, mac, nil); };
    self.sections = @[
        [PNSection header:nil rows:rows footer:nil],
        [PNSection header:nil rows:@[forget]
                   footer:@"Forgetting this Mac closes its connection. It will need to be paired again before it can run code here."],
    ];
}

@end

#pragma mark - Code that has run

@implementation PNCodeListViewController {
    NSArray *_items;
    id _lastCount;
    BOOL _loading;
}

- (void)viewDidLoad {
    [super viewDidLoad];
    self.title = @"Code that has run";
    self.navigationItem.largeTitleDisplayMode = UINavigationItemLargeTitleDisplayModeNever;
}

- (void)viewWillAppear:(BOOL)animated {
    _lastCount = nil;
    [super viewWillAppear:animated];
}

- (void)load {
    if (_loading) return;
    _loading = YES;
    [PNBridge call:@"code_list" arg:nil done:^(NSString *result) {
        self->_loading = NO;
        self->_items = [PNBridge json:result ofClass:[NSArray class]] ?: @[];
        [self render];
    }];
}

- (void)statusChanged {
    NSDictionary *st = PNStatus.shared.latest;
    // Reload when the count or the approvals change.
    id key = [NSString stringWithFormat:@"%@|%@|%@", st[@"code_count"], st[@"review"], st[@"waiting"]];
    if (!_lastCount || ![key isEqual:_lastCount]) {
        _lastCount = key;
        [self load];
    }
    if (_items) [self render];
}

static NSString *PNStateText(NSString *state) {
    NSDictionary *t = @{@"allowed": @"Allowed", @"waiting": @"Waiting for approval", @"denied": @"Not allowed",
                        @"not needed": @"No approval needed"};
    return t[state] ?: state;
}

- (void)render {
    BOOL review = [PNStatus.shared.latest[@"review"] boolValue];
    NSMutableArray *rows = [NSMutableArray array];
    __weak PNCodeListViewController *weak = self;
    for (id it in _items) {
        NSDictionary *item = PNDict(it);
        NSString *code = PNString(item[@"code"]);
        NSString *label = PNString(item[@"label"]).length ? PNString(item[@"label"]) : @"function";
        BOOL user = [PNString(item[@"origin"]) isEqualToString:@"user"];
        NSString *origin = user ? @"Your code"
            : PNString(item[@"dist"]).length ? [NSString stringWithFormat:@"Library: %@", PNString(item[@"dist"])] : @"Library";
        NSInteger runs = [item[@"runs"] integerValue];
        // Runs count tasks that finished here; code whose tasks were all stopped or cancelled
        // (the Mac's copy finished first) was sent but never ran.
        NSMutableArray *parts = [NSMutableArray arrayWithObject:origin];
        if (runs > 0) [parts addObject:runs == 1 ? @"ran once" : [NSString stringWithFormat:@"ran %ld times", (long)runs]];
        NSString *state = PNString(item[@"state"]);
        if (review && state.length) [parts addObject:PNStateText(state)];
        NSString *detail = [parts componentsJoinedByString:@", "];
        if (runs == 0) detail = [@"Sent, not run yet. " stringByAppendingString:detail];
        PNRow *r = [PNRow ident:[@"code:" stringByAppendingString:code] kind:PNRowSubtitle text:label detail:detail];
        r.disclosure = YES;
        NSMutableArray *head = [NSMutableArray arrayWithObject:origin];
        if (PNString(item[@"where"]).length) [head addObject:PNString(item[@"where"])];
        NSArray *macs = PNArray(item[@"macs"]);
        if (macs.count) [head addObject:[@"from " stringByAppendingString:[macs componentsJoinedByString:@", "]]];
        if ([PNString(item[@"srckind"]) isEqualToString:@"bytecode"])
            [head addObject:@"no source was available, so this is its bytecode"];
        if (review && user && state.length) [head addObject:PNStateText(state)];
        NSString *heading = [[head componentsJoinedByString:@", "] stringByAppendingString:@"."];
        BOOL decidable = review && user && code.length;
        r.action = ^{
            PNTextViewController *v = [[PNTextViewController alloc] initWithTitle:label func:@"code_source" arg:code];
            v.heading = heading;
            if (decidable) {
                __weak PNTextViewController *wv = v;
                v.decide = ^(BOOL allow) {
                    PNApprove(code, allow, ^{
                        [weak load];
                        [wv.navigationController popViewControllerAnimated:YES];
                    });
                };
            }
            [weak.navigationController pushViewController:v animated:YES];
        };
        [rows addObject:r];
    }
    if (!rows.count) [rows addObject:[PNRow ident:@"none" kind:PNRowNote text:@"No code has run yet." detail:nil]];
    self.sections = @[[PNSection header:nil rows:rows
        footer:@"Every function your Mac has sent to this iPhone since the app started, newest first. Tap one to read it."]];
}

@end

#pragma mark - Python files from the Mac

@implementation PNFetchedViewController {
    NSArray *_items;
}

- (void)viewDidLoad {
    [super viewDidLoad];
    self.title = @"Python files";
    self.navigationItem.largeTitleDisplayMode = UINavigationItemLargeTitleDisplayModeNever;
    [PNBridge call:@"fetched_json" arg:nil done:^(NSString *result) {
        self->_items = [PNBridge json:result ofClass:[NSArray class]] ?: @[];
        [self render];
    }];
}

- (void)render {
    NSMutableDictionary<NSString *, NSMutableArray *> *groups = [NSMutableDictionary dictionary];
    __weak PNFetchedViewController *weak = self;
    for (id it in _items) {
        NSDictionary *item = PNDict(it);
        NSString *path = PNString(item[@"path"]);
        if (!path.length) continue;
        NSString *module = PNString(item[@"module"]);
        NSString *top = (module.length ? [module componentsSeparatedByString:@"."] : path.pathComponents).firstObject;
        if ([top hasSuffix:@".py"]) top = [top stringByDeletingPathExtension];
        // The module's dotted name reads better than the cache path (which carries a hash).
        PNRow *r = [PNRow ident:[@"f:" stringByAppendingString:path] kind:PNRowSubtitle
                           text:module.length ? module : path detail:PNBytes(item[@"bytes"])];
        r.monospaced = YES;
        r.disclosure = YES;
        r.action = ^{
            PNTextViewController *v = [[PNTextViewController alloc] initWithTitle:path.lastPathComponent
                                                                             func:@"fetched_source" arg:path];
            v.heading = module.length ? [NSString stringWithFormat:@"%@ (%@)", path, module] : path;
            [weak.navigationController pushViewController:v animated:YES];
        };
        NSMutableArray *g = groups[top ?: @""];
        if (!g) groups[top ?: @""] = g = [NSMutableArray array];
        [g addObject:r];
    }
    NSMutableArray *sections = [NSMutableArray array];
    for (NSString *top in [groups.allKeys sortedArrayUsingSelector:@selector(localizedCaseInsensitiveCompare:)])
        [sections addObject:[PNSection header:top rows:groups[top] footer:nil]];
    if (!sections.count)
        [sections addObject:[PNSection header:nil rows:@[[PNRow ident:@"none" kind:PNRowNote
            text:@"No files have come from your Mac yet." detail:nil]] footer:nil]];
    PNSection *last = sections.lastObject;
    last.footer = @"Pure-Python modules this iPhone imported from your Mac while running its code. They are kept in the app's cache.";
    self.sections = sections;
}

@end

#pragma mark - Packages in this app

@implementation PNPackagesViewController

- (void)viewDidLoad {
    [super viewDidLoad];
    self.title = @"Packages";
    self.navigationItem.largeTitleDisplayMode = UINavigationItemLargeTitleDisplayModeNever;
}

- (void)statusChanged {
    NSDictionary *pkgs = PNDict(PNStatus.shared.latest[@"packages"]);
    NSMutableArray *rows = [NSMutableArray array];
    for (NSString *name in [pkgs.allKeys sortedArrayUsingSelector:@selector(localizedCaseInsensitiveCompare:)])
        [rows addObject:[PNRow ident:name kind:PNRowValue text:name detail:PNString(pkgs[name])]];
    if (!rows.count) [rows addObject:[PNRow ident:@"none" kind:PNRowNote text:@"No compiled packages." detail:nil]];
    self.sections = @[[PNSection header:@"Compiled packages in this app" rows:rows
        footer:@"These are built into the app and can't change after it is installed. A task that needs a compiled "
               @"package runs on this iPhone only when your Mac has the same release of it; otherwise it runs on the Mac."]];
}

@end
