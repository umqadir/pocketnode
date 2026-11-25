// The console: write Python on the phone and run it here, with or without a Mac.

#import "PNStoreUI.h"
#import "PNBridge.h"

static NSString *pn_console_code;  // what was in the editor, kept while the app runs

@interface PNConsoleViewController () <UITextViewDelegate>
@end

@implementation PNConsoleViewController {
    UITextView *_editor, *_output;
    UIBarButtonItem *_run, *_stop, *_examples;
    NSArray *_snippets;
    BOOL _wasRunning;
}

- (void)viewDidLoad {
    [super viewDidLoad];
    self.title = @"Python console";
    self.navigationItem.largeTitleDisplayMode = UINavigationItemLargeTitleDisplayModeNever;
    self.view.backgroundColor = [UIColor systemGroupedBackgroundColor];

    _editor = [UITextView new];
    _editor.font = PNMonoFont(UIFontTextStyleSubheadline);
    _editor.adjustsFontForContentSizeCategory = YES;
    _editor.textColor = [UIColor labelColor];
    _editor.backgroundColor = [UIColor secondarySystemGroupedBackgroundColor];
    _editor.layer.cornerRadius = 10;
    _editor.textContainerInset = UIEdgeInsetsMake(10, 6, 10, 6);
    _editor.autocorrectionType = UITextAutocorrectionTypeNo;
    _editor.autocapitalizationType = UITextAutocapitalizationTypeNone;
    _editor.spellCheckingType = UITextSpellCheckingTypeNo;
    _editor.smartQuotesType = UITextSmartQuotesTypeNo;
    _editor.smartDashesType = UITextSmartDashesTypeNo;
    _editor.smartInsertDeleteType = UITextSmartInsertDeleteTypeNo;
    _editor.inlinePredictionType = UITextInlinePredictionTypeNo;
    _editor.keyboardDismissMode = UIScrollViewKeyboardDismissModeInteractive;
    _editor.accessibilityLabel = @"Code";
    _editor.delegate = self;
    _editor.text = pn_console_code ?: @"";

    UILabel *outLabel = [UILabel new];
    outLabel.text = @"Output";
    outLabel.font = [UIFont preferredFontForTextStyle:UIFontTextStyleFootnote];
    outLabel.adjustsFontForContentSizeCategory = YES;
    outLabel.textColor = [UIColor secondaryLabelColor];
    outLabel.accessibilityTraits = UIAccessibilityTraitHeader;

    _output = [UITextView new];
    _output.editable = NO;
    _output.selectable = YES;
    _output.adjustsFontForContentSizeCategory = YES;
    _output.backgroundColor = [UIColor secondarySystemGroupedBackgroundColor];
    _output.layer.cornerRadius = 10;
    _output.textContainerInset = UIEdgeInsetsMake(10, 6, 10, 6);
    _output.accessibilityLabel = @"Output";

    UIStackView *stack = [[UIStackView alloc] initWithArrangedSubviews:@[_editor, outLabel, _output]];
    stack.axis = UILayoutConstraintAxisVertical;
    stack.spacing = 6;
    [stack setCustomSpacing:14 afterView:_editor];
    stack.translatesAutoresizingMaskIntoConstraints = NO;
    [self.view addSubview:stack];
    UILayoutGuide *safe = self.view.safeAreaLayoutGuide;
    NSLayoutConstraint *split = [_editor.heightAnchor constraintEqualToAnchor:_output.heightAnchor multiplier:1.3];
    split.priority = UILayoutPriorityDefaultHigh;
    [NSLayoutConstraint activateConstraints:@[
        [stack.leadingAnchor constraintEqualToAnchor:self.view.layoutMarginsGuide.leadingAnchor],
        [stack.trailingAnchor constraintEqualToAnchor:self.view.layoutMarginsGuide.trailingAnchor],
        [stack.topAnchor constraintEqualToAnchor:safe.topAnchor constant:8],
        [stack.bottomAnchor constraintEqualToAnchor:self.view.keyboardLayoutGuide.topAnchor constant:-8],
        [_output.heightAnchor constraintGreaterThanOrEqualToConstant:60],
        split,
    ]];

    __weak PNConsoleViewController *weak = self;
    _run = [[UIBarButtonItem alloc] initWithPrimaryAction:
        [UIAction actionWithTitle:@"Run" image:[UIImage systemImageNamed:@"play.fill"] identifier:nil
                          handler:^(UIAction *a) { [weak run]; }]];
    _stop = [[UIBarButtonItem alloc] initWithPrimaryAction:
        [UIAction actionWithTitle:@"Stop" image:[UIImage systemImageNamed:@"stop.fill"] identifier:nil
                          handler:^(UIAction *a) { [PNLocalRun.shared stop]; }]];
    _stop.tintColor = [UIColor systemRedColor];
    _examples = [[UIBarButtonItem alloc] initWithImage:[UIImage systemImageNamed:@"text.badge.plus"] menu:nil];
    _examples.title = @"Examples";
    _examples.accessibilityLabel = @"Examples";
    _examples.enabled = NO;

    [[NSNotificationCenter defaultCenter] addObserver:self selector:@selector(update)
                                                 name:PNLocalRunDidChange object:nil];
    [PNBridge call:@"demo_snippets" arg:nil done:^(NSString *result) {
        [weak gotSnippets:[PNBridge json:result ofClass:[NSArray class]]];
    }];
    [self update];
}

- (void)gotSnippets:(NSArray *)snippets {
    NSMutableArray *ok = [NSMutableArray array];
    for (id s in snippets)
        if (PNString(PNDict(s)[@"code"]).length) [ok addObject:s];
    _snippets = ok;
    if (!ok.count) return;
    if (!pn_console_code.length && !_editor.text.length) {
        _editor.text = PNString(PNDict(ok[0])[@"code"]);
        pn_console_code = _editor.text;
    }
    NSMutableArray *actions = [NSMutableArray array];
    __weak PNConsoleViewController *weak = self;
    for (NSDictionary *s in ok) {
        NSString *code = PNString(s[@"code"]);
        NSString *title = PNString(s[@"title"]).length ? PNString(s[@"title"]) : @"Example";
        [actions addObject:[UIAction actionWithTitle:title image:nil identifier:nil handler:^(UIAction *a) {
            [weak load:code];
        }]];
    }
    _examples.menu = [UIMenu menuWithTitle:@"Load an example" children:actions];
    _examples.enabled = YES;
}

- (void)load:(NSString *)code {
    _editor.text = code;
    pn_console_code = code;
    [_editor setContentOffset:CGPointZero animated:NO];
}

- (void)textViewDidChange:(UITextView *)textView {
    pn_console_code = textView.text;
}

- (void)run {
    [_editor resignFirstResponder];
    if (!_editor.text.length) return;
    [PNLocalRun.shared run:_editor.text];
}

- (void)update {
    PNLocalRun *run = PNLocalRun.shared;
    self.navigationItem.rightBarButtonItems = @[run.running ? _stop : _run, _examples];
    _editor.editable = !run.running;

    UIFont *mono = PNMonoFont(UIFontTextStyleFootnote);
    NSMutableAttributedString *s = [NSMutableAttributedString new];
    if (run.output.length) {
        [s appendAttributedString:[[NSAttributedString alloc] initWithString:run.output
            attributes:@{NSFontAttributeName: mono, NSForegroundColorAttributeName: [UIColor labelColor]}]];
        if (![run.output hasSuffix:@"\n"]) [s appendAttributedString:[[NSAttributedString alloc] initWithString:@"\n"]];
    }
    BOOL stopped = [run.error isEqualToString:@"stopped"];
    if (run.error.length && !stopped) {
        [s appendAttributedString:[[NSAttributedString alloc] initWithString:[run.error stringByAppendingString:@"\n"]
            attributes:@{NSFontAttributeName: mono, NSForegroundColorAttributeName: [UIColor systemRedColor]}]];
    }
    NSString *footer = nil;
    if (run.running) footer = @"Running…";
    else if (run.finished) {
        NSString *t = [NSString stringWithFormat:@"%.3f s", run.seconds];
        footer = stopped ? [@"Stopped after " stringByAppendingString:t]
               : run.error.length ? [@"Failed after " stringByAppendingString:t]
                                  : [@"Finished in " stringByAppendingString:t];
    } else {
        footer = @"Tap Run to run the code on this iPhone.";
    }
    [s appendAttributedString:[[NSAttributedString alloc] initWithString:footer
        attributes:@{NSFontAttributeName: [UIFont preferredFontForTextStyle:UIFontTextStyleFootnote],
                     NSForegroundColorAttributeName: [UIColor secondaryLabelColor]}]];
    BOOL atEnd = _output.contentOffset.y + _output.bounds.size.height >= _output.contentSize.height - 20;
    _output.attributedText = s;
    if (atEnd && run.running) [_output scrollRangeToVisible:NSMakeRange(s.length, 0)];
    if (_wasRunning && !run.running) UIAccessibilityPostNotification(UIAccessibilityAnnouncementNotification, footer);
    _wasRunning = run.running;
}

@end
