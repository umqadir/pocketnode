// Rows, sections and the table controller the store screens are built from, and helpers.

#import "PNStoreUI.h"
#import "PNBridge.h"

#pragma mark - Helpers

NSString *PNDateText(id epoch) {
    if (![epoch isKindOfClass:[NSNumber class]] || [epoch doubleValue] <= 0) return nil;
    static NSDateFormatter *f;
    static dispatch_once_t once;
    dispatch_once(&once, ^{
        f = [NSDateFormatter new];
        f.dateStyle = NSDateFormatterMediumStyle;
        f.timeStyle = NSDateFormatterShortStyle;
        f.doesRelativeDateFormatting = YES;
    });
    return [f stringFromDate:[NSDate dateWithTimeIntervalSince1970:[epoch doubleValue]]];
}

NSString *PNDuration(double seconds) {
    long s = (long)MAX(0, seconds);
    if (s < 60) return [NSString stringWithFormat:@"%ld s", s];
    if (s < 3600) return [NSString stringWithFormat:@"%ld:%02ld", s / 60, s % 60];
    return [NSString stringWithFormat:@"%ld:%02ld:%02ld", s / 3600, (s / 60) % 60, s % 60];
}

UIFont *PNMonoFont(UIFontTextStyle style) {
    UITraitCollection *base = [UITraitCollection traitCollectionWithPreferredContentSizeCategory:UIContentSizeCategoryLarge];
    CGFloat size = [UIFontDescriptor preferredFontDescriptorWithTextStyle:style compatibleWithTraitCollection:base].pointSize;
    UIFont *mono = [UIFont monospacedSystemFontOfSize:size weight:UIFontWeightRegular];
    return [[UIFontMetrics metricsForTextStyle:style] scaledFontForFont:mono];
}

void PNConfirmForget(UIViewController *vc, NSDictionary *mac, void (^done)(void)) {
    NSString *pairId = PNString(mac[@"pair_id"]);
    NSString *name = PNString(mac[@"name"]).length ? PNString(mac[@"name"]) : @"this Mac";
    UIAlertController *a = [UIAlertController
        alertControllerWithTitle:[NSString stringWithFormat:@"Forget “%@”?", name]
                         message:@"It will need to be paired again before it can run code here."
                  preferredStyle:UIAlertControllerStyleAlert];
    [a addAction:[UIAlertAction actionWithTitle:@"Cancel" style:UIAlertActionStyleCancel handler:nil]];
    [a addAction:[UIAlertAction actionWithTitle:@"Forget This Mac" style:UIAlertActionStyleDestructive
                                        handler:^(UIAlertAction *x) {
        [PNBridge call:@"forget" arg:pairId done:^(NSString *r) {
            [PNStatus.shared refreshNow];
            if (done) done();
        }];
    }]];
    [vc presentViewController:a animated:YES completion:nil];
}

void PNApprove(NSString *code, BOOL allow, void (^done)(void)) {
    NSString *arg = [NSString stringWithFormat:@"%@ %@", code, allow ? @"1" : @"0"];
    [PNBridge call:@"approve" arg:arg done:^(NSString *r) {
        [PNStatus.shared refreshNow];
        if (done) done();
    }];
}

#pragma mark - Rows

@implementation PNRow

+ (instancetype)ident:(NSString *)ident kind:(PNRowKind)kind text:(NSString *)text detail:(NSString *)detail {
    PNRow *r = [PNRow new];
    r.ident = ident;
    r.kind = kind;
    r.text = text ?: @"";
    r.detail = detail;
    r.enabled = YES;
    return r;
}

@end

@implementation PNSection

+ (instancetype)header:(NSString *)header rows:(NSArray<PNRow *> *)rows footer:(NSString *)footer {
    PNSection *s = [PNSection new];
    s.header = header;
    s.rows = rows;
    s.footer = footer;
    return s;
}

@end

@interface PNSwitch : UISwitch
@property (copy) NSString *ident;
@end

@implementation PNSwitch
@end

#pragma mark - Table

@implementation PNTableViewController {
    NSArray<PNSection *> *_pending;
    NSMutableDictionary<NSString *, NSDictionary *> *_held;  // switches just flipped: {on, at}; status may lag
    BOOL _swiping;
}

- (instancetype)init {
    self = [super initWithStyle:UITableViewStyleInsetGrouped];
    _held = [NSMutableDictionary dictionary];
    _sections = @[];
    return self;
}

- (void)viewDidLoad {
    [super viewDidLoad];
    [self.tableView registerClass:[UITableViewCell class] forCellReuseIdentifier:@"cell"];
}

- (void)viewWillAppear:(BOOL)animated {
    [super viewWillAppear:animated];
    [[NSNotificationCenter defaultCenter] addObserver:self selector:@selector(statusChanged)
                                                 name:PNStatusDidChange object:nil];
    [self statusChanged];
}

- (void)viewDidDisappear:(BOOL)animated {
    [super viewDidDisappear:animated];
    [[NSNotificationCenter defaultCenter] removeObserver:self name:PNStatusDidChange object:nil];
}

- (void)statusChanged {
}

static NSString *signature(NSArray<PNSection *> *sections) {
    NSMutableString *s = [NSMutableString string];
    for (PNSection *sec in sections) {
        [s appendFormat:@"[%@|%@]", sec.header ?: @"", sec.footer ?: @""];
        for (PNRow *r in sec.rows) [s appendFormat:@"%@:%ld,", r.ident, (long)r.kind];
    }
    return s;
}

- (void)setSections:(NSArray<PNSection *> *)sections {
    BOOL same = [signature(_sections) isEqualToString:signature(sections)];
    if (!same && _swiping) { _pending = sections; return; }
    _sections = sections;
    _pending = nil;
    if (!self.isViewLoaded) return;
    if (!same) { [self.tableView reloadData]; return; }
    BOOL resize = NO;
    for (NSIndexPath *ip in self.tableView.indexPathsForVisibleRows) {
        UITableViewCell *cell = [self.tableView cellForRowAtIndexPath:ip];
        if (cell) resize |= [self configure:cell row:[self rowAt:ip]];
    }
    if (resize) [UIView performWithoutAnimation:^{ [self.tableView performBatchUpdates:nil completion:nil]; }];
}

- (PNRow *)rowAt:(NSIndexPath *)ip {
    if (ip.section >= (NSInteger)_sections.count) return nil;
    NSArray *rows = _sections[ip.section].rows;
    return ip.row < (NSInteger)rows.count ? rows[ip.row] : nil;
}

- (PNRow *)rowWithIdent:(NSString *)ident {
    for (PNSection *s in _sections)
        for (PNRow *r in s.rows)
            if ([r.ident isEqualToString:ident]) return r;
    return nil;
}

// Returns whether the text changed (the row may need a new height).
- (BOOL)configure:(UITableViewCell *)cell row:(PNRow *)row {
    if (!row) return NO;
    UIListContentConfiguration *old = [cell.contentConfiguration isKindOfClass:[UIListContentConfiguration class]]
        ? (UIListContentConfiguration *)cell.contentConfiguration : nil;
    BOOL changed = !old || ![(old.text ?: @"") isEqualToString:row.text ?: @""] ||
                   ![(old.secondaryText ?: @"") isEqualToString:row.detail ?: @""];
    UIListContentConfiguration *c;
    switch (row.kind) {
        case PNRowValue: c = [UIListContentConfiguration valueCellConfiguration]; break;
        case PNRowSubtitle: c = [UIListContentConfiguration subtitleCellConfiguration]; break;
        default: c = [UIListContentConfiguration cellConfiguration]; break;
    }
    c.text = row.text;
    c.secondaryText = row.detail;
    if (row.kind == PNRowSubtitle) c.secondaryTextProperties.color = [UIColor secondaryLabelColor];
    if (row.monospaced) c.textProperties.font = PNMonoFont(UIFontTextStyleBody);
    switch (row.kind) {
        case PNRowButton:
            c.textProperties.color = row.enabled ? [UIColor tintColor] : [UIColor tertiaryLabelColor];
            break;
        case PNRowDestructive:
            c.textProperties.color = row.enabled ? [UIColor systemRedColor] : [UIColor tertiaryLabelColor];
            break;
        case PNRowNote:
            c.textProperties.color = [UIColor secondaryLabelColor];
            break;
        default:
            if (row.action && !row.enabled) {
                c.textProperties.color = [UIColor tertiaryLabelColor];
                c.secondaryTextProperties.color = [UIColor tertiaryLabelColor];
            }
            break;
    }
    cell.contentConfiguration = c;

    UIAccessibilityTraits traits = UIAccessibilityTraitNone;
    if (row.kind == PNRowToggle) {
        PNSwitch *sw = [cell.accessoryView isKindOfClass:[PNSwitch class]] ? (PNSwitch *)cell.accessoryView : nil;
        if (!sw) {
            sw = [PNSwitch new];
            [sw addTarget:self action:@selector(switched:) forControlEvents:UIControlEventValueChanged];
            cell.accessoryView = sw;
        }
        sw.ident = row.ident;
        // The person's choice shows until status reports it, or for 3 s (then status wins).
        NSDictionary *held = _held[row.ident];
        BOOL show = row.on;
        if (held) {
            BOOL choice = [held[@"on"] boolValue];
            if (choice == row.on || -[held[@"at"] timeIntervalSinceNow] > 3.0) [_held removeObjectForKey:row.ident];
            else show = choice;
        }
        if (sw.on != show) [sw setOn:show animated:old != nil];
        sw.enabled = row.enabled;
        sw.accessibilityLabel = row.text;
        sw.accessibilityHint = row.hint;
        cell.accessoryType = UITableViewCellAccessoryNone;
        cell.selectionStyle = UITableViewCellSelectionStyleNone;
    } else {
        cell.accessoryView = nil;
        cell.accessoryType = row.disclosure ? UITableViewCellAccessoryDisclosureIndicator : UITableViewCellAccessoryNone;
        BOOL tappable = row.action && row.enabled;
        cell.selectionStyle = tappable ? UITableViewCellSelectionStyleDefault : UITableViewCellSelectionStyleNone;
        if (row.action) traits |= UIAccessibilityTraitButton;
        if (row.action && !row.enabled) traits |= UIAccessibilityTraitNotEnabled;
    }
    cell.accessibilityTraits = traits;
    cell.accessibilityHint = row.kind == PNRowToggle ? nil : row.hint;
    return changed;
}

- (void)releaseSwitch:(NSString *)ident {
    [_held removeObjectForKey:ident];
}

- (void)switched:(PNSwitch *)sw {
    PNRow *row = [self rowWithIdent:sw.ident];
    if (!row.toggled) return;
    _held[sw.ident] = @{@"on": @(sw.on), @"at": [NSDate date]};
    row.on = sw.on;
    row.toggled(sw.on, sw);
}

#pragma mark Data source and delegate

- (NSInteger)numberOfSectionsInTableView:(UITableView *)tableView {
    return _sections.count;
}

- (NSInteger)tableView:(UITableView *)tableView numberOfRowsInSection:(NSInteger)section {
    return _sections[section].rows.count;
}

- (NSString *)tableView:(UITableView *)tableView titleForHeaderInSection:(NSInteger)section {
    return _sections[section].header;
}

- (NSString *)tableView:(UITableView *)tableView titleForFooterInSection:(NSInteger)section {
    return _sections[section].footer;
}

- (UITableViewCell *)tableView:(UITableView *)tableView cellForRowAtIndexPath:(NSIndexPath *)ip {
    UITableViewCell *cell = [tableView dequeueReusableCellWithIdentifier:@"cell" forIndexPath:ip];
    [self configure:cell row:[self rowAt:ip]];
    return cell;
}

- (BOOL)tableView:(UITableView *)tableView shouldHighlightRowAtIndexPath:(NSIndexPath *)ip {
    PNRow *r = [self rowAt:ip];
    return r.action && r.enabled && r.kind != PNRowToggle;
}

- (void)tableView:(UITableView *)tableView didSelectRowAtIndexPath:(NSIndexPath *)ip {
    [tableView deselectRowAtIndexPath:ip animated:YES];
    PNRow *r = [self rowAt:ip];
    if (r.action && r.enabled) r.action();
}

- (UISwipeActionsConfiguration *)tableView:(UITableView *)tableView
    trailingSwipeActionsConfigurationForRowAtIndexPath:(NSIndexPath *)ip {
    PNRow *r = [self rowAt:ip];
    if (!r.deleted) return nil;
    void (^deleted)(void) = r.deleted;
    UIContextualAction *a = [UIContextualAction contextualActionWithStyle:UIContextualActionStyleDestructive
        title:r.deleteTitle ?: @"Delete" handler:^(UIContextualAction *x, UIView *v, void (^completion)(BOOL)) {
        completion(NO);  // the row goes when status says so, after the confirmation
        deleted();
    }];
    UISwipeActionsConfiguration *c = [UISwipeActionsConfiguration configurationWithActions:@[a]];
    c.performsFirstActionWithFullSwipe = NO;
    return c;
}

- (void)tableView:(UITableView *)tableView willBeginEditingRowAtIndexPath:(NSIndexPath *)ip {
    _swiping = YES;
}

- (void)tableView:(UITableView *)tableView didEndEditingRowAtIndexPath:(NSIndexPath *)ip {
    _swiping = NO;
    if (_pending) self.sections = _pending;
}

@end
