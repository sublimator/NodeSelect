#coding: utf8
#################################### IMPORTS ###################################

# Std Libs
import bisect
import re
import time
import threading

from collections import defaultdict, deque
from itertools import chain
from functools import partial

# 3rd Party Libs
from lxml import etree as ET, html
from cssselect import GenericTranslator

# Sublime Libs
import sublime
import sublime_plugin

# Package helper libs
from .trackers import back_track, track_regex
from .scopedtokenizer import crude_tokenizer, scoped_tokenizer

################################### CONSTANTS ##################################

# In case we don't really have an explicit root.
AUTO_ROOT_OPEN_TAG  = (b'<sublime:document '
                       b'xmlns:sublime="http://www.sublimetext.com">')
AUTO_ROOT_CLOSE_TAG = b'</sublime:document>'
# What if you have a bunch of tags in a document and one of them actually starts
# at 0, it would ruin the bisection mechanisms if some automatic root started @
# 0 too.
AUTO_ROOT_START     = -1

# If we don't have a doctype we'll feed this
DEFAULT_DOCTYPE     = ( b'<!DOCTYPE html>' )
MODULE_LOAD_TIME    = time.time()
NON_TAGS            = (ET._Comment, ET._ProcessingInstruction)
HANDLE_ENTITIES     = re.compile("&(\w+);").sub
XMLNS               = re.compile(r'xmlns=("|\').*?\1')
XPATH_ATTRS         = re.compile("/@([^ ]+)(?: |$)")

XPATH_NAMESPACES    =  {
    'xi': 'http://www.w3.org/2001/XInclude',
    'py': 'http://genshi.edgewall.org/',
    're':'http://exslt.org/regular-expressions'
}

# Used for View data
KEY                 = __package__

# Recovery mode parsing should sort thiese, but this makes it explicit and works
# better with NodeProxy
SELF_CLOSING_DIDNT_EXPLICITLY_CLOSE        = re.compile (
    '<(area|base|basefont|br|col|frame|hr|img|input|isindex|link|meta|param|'
      'embed|keygen,command)(>|.*?[^/]>)')

################################## EXCEPTIONS ##################################

class Bailed(Exception):
    """
    [ab]used to break through multiple loops
    """

#################################### HELPERS ###################################

def find_tag_start(view, start_pt):
    regions = back_track(view, start_pt, track_regex('<', False) )
    return regions[-1].begin()

def timeout(f):
    sublime.set_timeout(f, 10)

def shrink_wrap_region( view, region ):
    a, b = region.begin(), region.end()

    for a in range(a, b):
        if not view.substr(a).isspace():
            break

    for b in range(b-1, a, -1):
        if not view.substr(b).isspace():
            b += 1
            break

    return sublime.Region(a, b)

################################## VIEW PROXY ##################################

class NodeProxy:
    def __init__(self, view, first_500, xml):
        self.first_500_chars_lowered = first_500.lower()
        self.xml = xml

        self.positions = []
        self.opened = defaultdict(list)
        self.regions = {}
        self.tags_lookup = {}
        self.view = view
        self.start_pos = AUTO_ROOT_START
        self.end_pos = AUTO_ROOT_START
        self.root = None

    def start(self, tag, attrib):
        start = self.start_pos
        self.positions.append(start)
        self.opened[tag].append((start, self.end_pos))

    def end(self, tag):
        start, end = self.opened[tag].pop()
        node = sublime.Region( start,  self.end_pos )
        node.starts = sublime.Region( start,  end )
        node.ends   = sublime.Region( self.start_pos,  self.end_pos )
        self.regions[start] = node

    def pi(self, lang, data):
        self.add_node()

    def comment(self, data):
        self.add_node()

    def add_node(self):
        if not self.opened: return
        start, end = self.start_pos, self.end_pos
        self.positions.append(start)

        node = sublime.Region(start, end)
        node.starts = node.ends = node
        self.regions[start] = node

    def create_mapping_parser(self):
        Parser = ET.XMLParser if self.xml else  html.XHTMLParser
        return Parser(target=self,
                load_dtd=False, no_network=True, resolve_entities=False,
                recover=True,
            )

    def create_root_parser(self):
        if self.xml:
            return ET.XMLParser(strip_cdata=False )
        else:
            return html.XHTMLParser (
                strip_cdata=True,
                load_dtd=False,
                recover=True,
                resolve_entities=False,
                no_network=True )

    def create_feed_routine(self):
        """
        
        Co-routines are actually measurably faster than keeping state in
        instance variables, so fuck it, why not use one eh?

        """
        # t = time.time()

        parser         = self.create_mapping_parser()

        # TODO: offload this into another thread ... or ... manually build a
        # DOM?
        bparser        = self.create_root_parser()

        has_doctype = '<!DOCTYPE'.lower() in self.first_500_chars_lowered
        # For buffers with no explicit root (Templates / PHP etc)
        self.auto_root = auto_root = int(
                not self.xml and not has_doctype
                and '<html' not in self.first_500_chars_lowered)

        # This bollix is for when the buffer has no `root` node per se lxml is
        # quite strict in wanting a root node
        if auto_root:
            for p in parser, bparser:
                if not has_doctype:
                    p.feed((DEFAULT_DOCTYPE))
                p.feed(AUTO_ROOT_OPEN_TAG)

        while True:
            # First time you send(None) it will run until this yield point,
            # yielding None. It then halts, waiting for `send(token)`
            val = (yield)

            if val is False:  return # Buffer was modified
            elif val is True: break  # No more tokens to feed
            else:             token, self.start_pos, self.end_pos  = val

            # TODO: move these two to scoped_tokenizer
            if SELF_CLOSING_DIDNT_EXPLICITLY_CLOSE.match(token):
                token = token[:-1] + ' />'
            elif token.startswith("<!doctype"): # fix for html 5
                token = "<!DOCTYPE" + token[9:]
            try:
                encode = token.encode('utf-8')
                parser.feed(encode)
                bparser.feed(encode)
            except ET.XMLSyntaxError as e:
                print (e)
                yield e

        if auto_root:
            for p in parser, bparser: p.feed(AUTO_ROOT_CLOSE_TAG)

        self.root = bparser.close()
        parser.close()

        # print ("Time to dom", time.time() - t)
        yield self.create_lookup()

    def create_lookup(self):
        if self.root is None: return False

        for t in self.root.iter():
            if isinstance(t, ET._Entity):
                t.getparent().remove(t)

        for i, tag in enumerate(t for t in self.root.iter() if
                  not isinstance(t, ET._Entity)):
            self.tags_lookup[tag] = i
            self.tags_lookup[i]   = tag

        # if any(self.opened.values()):
        #     print ("Opened values", self.opened.values())

        # print ("Returning True")
        return True


    def close(self):
        pass

    def node_region(self, e):
        return self.regions[self.positions[self.tags_lookup[e]]]

    def node_starts(self, e):
        return self.positions[self.tags_lookup[e]]

    def __getitem__(self, index):
        return self.regions[self.positions[index]]

################################### VIEW DATA ##################################

class bunch(dict):
    def __init__(self, *args, **kw):
        dict.__init__(self, *args, **kw)
        self.__dict__ = self

class ViewData(sublime_plugin.EventListener):
    """
    TODO:
    
        5 minute job. Probably leaky ...
    
    """
    oneoff_callbacks = defaultdict(lambda: defaultdict(deque))
    buffer_data      = defaultdict(lambda: [1, defaultdict(bunch)])
    data             = defaultdict(lambda: defaultdict(bunch))

    # Oone shot callbacks, very useful!
    @classmethod
    def add_oneoff_callback(cls, view_id, key, cb):
        cls.oneoff_callbacks[view_id][key].append(cb)

    def on_clone(self, view):
        ViewData.buffer_data[view.buffer_id()][0] += 1

    def on_modified(self, v):
        callbacks = ViewData.oneoff_callbacks[v.view_id]['on_modified']
        if callbacks:
            while callbacks:
                callbacks.popleft()(v)

    def on_close(self, v):
        def after():
            buffer_id = v.buffer_id()

            try:
                del ViewData.data[v.view_id]
            except KeyError:
                pass
            try:
                del ViewData.oneoff_callbacks[v.view_id]
            except KeyError:
                pass

            d = ViewData.buffer_data[buffer_id]
            d[0] -= 1
            if d[0] < 1:
                try:
                    del ViewData.buffer_data[buffer_id]
                except Exception:
                    print ("Error cleaning up View buffer data")
        sublime.set_timeout(after)

#################################### HELPERS ###################################

def selection_nodes(view, sels = None, node_proxy=None):
    node_proxy = node_proxy or (ViewData.buffer_data[view.buffer_id()][1][KEY]
                                        .get('node_proxy'))

    if node_proxy is None:
        ProxyBuilder().trigger(view)
        return [], node_proxy

    node_starts = node_proxy.positions
    nodes = []

    for sel in (sels or view.sel()):
        node_index = max (
            0, bisect.bisect(node_starts, sel.begin() ) -1 )
        node = node_proxy[node_index]

        if node is not None:
            while not node.contains(sel):
                parent = node_proxy.tags_lookup[node_index].getparent()
                if parent is None: break

                node_index = node_proxy.tags_lookup[parent]
                node = node_proxy[node_index]

            # if node is not None:
            # if node_proxy.auto_root and node.begin() == AUTO_ROOT_START:
            #     node = sublime.Region(0, 0)# view.size())
            #     print ("SelectionNodes", node)
                # node = None
            # else:
            nodes.append((node_index, node))

    return nodes, node_proxy

def element_name_region(view, pt):
    "Return element name region from region where begin() is <"

    region = view.extract_scope( pt + 1 )
    start = view.find('[^<]', pt).begin()
    return sublime.Region(start, region.end())

def element_name_regions(view, node, ix=None, node_proxy=None):
    if ix is not None:
        p = node_proxy.tags_lookup[ix]
        if isinstance(p, NON_TAGS):
            if p.getparent() is not None:
                 return [node_proxy.node_region(p)]

    ns = [element_name_region(view, node.begin())]

    if node.starts.end() != node.ends.end():
        ns += [view.extract_scope(node.end() -2)]

    return ns

def css_to_xpath(s, only_descendants=False):
    prefix = ( 'descendant::' if only_descendants else 'descendant-or-self::' )
    return (GenericTranslator().css_to_xpath(s, prefix=prefix))

def xpath_attribute_regions(view, element, tag_starts, xpath, result):
    attrs = XPATH_ATTRS.findall(xpath)

    if '*' in attrs: attrs = list(element.keys())
    items = list(element.items())

    regions = []

    for attr in attrs:
        if not (attr, result) in items: continue
        begin = view.find( r"""(?s)\b%s\s*?=\s*?('|")""" % attr,
                                    tag_starts ).end()

        end = view.find(view.substr(begin -1), begin ).end() - 1

        regions.append(sublime.Region(begin, end))

    return regions

def xpath_tail_region(view, node_proxy, element):
    start   = node_proxy.node_region(element).end()
    sibling = element.getnext()
    parent  = element.getparent()

    if sibling is not None:
        end = node_proxy.node_region(sibling).begin()

    elif parent is not None:
        end = node_proxy.node_region(parent).ends.begin()

    return sublime.Region(start, end)

def xpath_text_region(view, node_proxy, element):
    element_region = node_proxy.node_region(element)
    start = element_region.starts.end()

    if len(element):
        first_child = element[0]
        end = node_proxy.node_region(first_child).begin()
    else:
        end = element_region.ends.begin()

    return sublime.Region(start, end)

def xp_2_selections(view, node_proxy, xp, p, full=False):
    if isinstance(p, str):
        parent = p.getparent()
        start = node_proxy.node_starts(parent)

        if p.is_attribute:
            return xpath_attribute_regions(view, parent, start, xp, p)

        elif p.is_tail:
            return [xpath_tail_region(view, node_proxy, parent)]

        elif p.is_text:
            return [xpath_text_region(view, node_proxy, parent)]

    elif isinstance(p,  NON_TAGS):
        if p.getparent() is not None:
             return [node_proxy.node_region(p)]
    else:
        if full:
            return [node_proxy.node_region(p)]
        else:
            return [element_name_region( view, node_proxy.node_starts(p) )]

################################## XPATH MIXIN #################################

class ShowsXPathMixin:
    def show_xpath(self, view, node_proxy, start_mod=None, threaded=False):
        sels = view.sel()
        if not sels:return

        sela = sels[0]
        if view.match_selector(sela.a, 'text.html, text.xml'):

            if node_proxy is not None:
                try:
                    nodes, node_proxy = selection_nodes(view,
                                                        sels=sels,
                                                        node_proxy=node_proxy)
                    if nodes:
                        i, node = list(nodes)[0]
                        
                        if node.a != AUTO_ROOT_START:
                            highlights = list (
                                chain(*( element_name_regions (
                                            view, n, ix=i, node_proxy=node_proxy)
                                          for (i, n) in nodes )))

                            if threaded and not view.change_count() == start_mod:
                                raise Exception()

                            view.add_regions('xpath',   highlights,
                                             'comment', flags= sublime.DRAW_NO_OUTLINE |  sublime.DRAW_NO_FILL | sublime.DRAW_SOLID_UNDERLINE | sublime.DRAW_EMPTY_AS_OVERWRITE)
                        xpath = (node_proxy.root
                                           .getroottree()
                                           .getpath(node_proxy.tags_lookup[i]))

                        view.set_status('xpath', xpath)
                except Exception:
                    # Nasty yes, but we get issues reasonably often
                    return

################################## SHOW XPATH ##################################

class ShowXPath(sublime_plugin.EventListener, ShowsXPathMixin):
    def on_selection_modified_async(self, view):
        """
        We don't want to schedule these up too many times as it becomes a real
        PITA.
        
        """
        view_data = ViewData.data[view.view_id][KEY]

        if view_data.get('already_on_selection_modified'):
            return False
        else:
            try:
                view_data.already_on_selection_modified = True
                node_proxy = ViewData.buffer_data[view.buffer_id()][1][KEY].get("node_proxy")
                if node_proxy is not None:
                    self.show_xpath(view, node_proxy)
                else:
                    ProxyBuilder().trigger(view)
            finally:
                view_data.already_on_selection_modified = False

################################## PROXY CACHE #################################

class ProxyBuilder(sublime_plugin.EventListener, ShowsXPathMixin):
    def on_query_context(self, view, key, op, operand, match_all):
        if key == 'selections_are_nodes':
            start_sels            = list(view.sel())
            node_sels, node_proxy = selection_nodes(view, start_sels)
            if node_proxy is None: return False
            return all(
                (sr == nr for (sr, (i, nr)) in zip(start_sels, node_sels))
            )

    def on_modified_async(self, view):
        if not view.match_selector(0, 'text.html'):
            return

        # Get the view data related to NodeSelect
        view_data = ViewData.buffer_data[view.buffer_id()][1][KEY]
        # Invalidate any node_proxy that has been built up
        view_data.node_proxy = None

        try:
            view_data.select_node_thread
        except AttributeError:
            t = view_data.select_node_thread = threading.Thread(
                    target=self.thread_loop, args=(view, view_data))
            t.start()

    trigger = on_modified_async

    def thread_loop(self, view, view_data):
        thread_started_at = time.time()

        while True:
            try:
                start_mod = view.change_count()
                ev = threading.Event()
                ViewData.add_oneoff_callback(
                        view.view_id, 'on_modified', lambda v: ev.set())

                substr     = view.substr(sublime.Region(0, view.size()))
                node_proxy = NodeProxy(view, substr[:500], xml= False)
                feeder     = node_proxy.create_feed_routine()
                feed       = feeder.send
                feed(None)

                for token in scoped_tokenizer(view, crude_tokenizer(substr)):
                    if view.change_count() > start_mod:
                        raise Bailed
                    else:
                        if isinstance(feed(token), ET.XMLSyntaxError):
                            raise Bailed

                # Finish up, turning any gears left in the machine
                while True:
                    try:
                        if feed(True) is False: raise Bailed
                    except StopIteration:
                        break

                # Did we successfully build a tree?
                if node_proxy.root is not None:
                    view_data.node_proxy = node_proxy
                    self.show_xpath(view, node_proxy, start_mod, threaded=True)
                else:
                    view_data.node_proxy = None

            except Bailed:
                "We just wait for the next modification"

            # Development convenience
            if thread_started_at < MODULE_LOAD_TIME:
                del view_data.select_node_thread
                break

            # We wait 30 seconds, otherwise relinquish the thread
            was_modified = ev.wait(timeout=30)
            if not was_modified:
                # Clean up reference to this thread in the view data
                del view_data.select_node_thread
                break

################################### COMMANDS ###################################

class NodeSelectRegions(sublime_plugin.TextCommand):
    def run(self, edit, regions, show_surrounds=True):
        view = self.view
        view.sel().clear()
        for r in regions:
            view.sel().add(sublime.Region(*r))

        view.show(view.sel(), show_surrounds)

class PathSelect(sublime_plugin.TextCommand):
    css = ''
    xpath = ''


    def create_xpath(self, s, css_select=False, only_descendants=False):
        try:
            if css_select:
                xp = css_to_xpath(s, only_descendants=only_descendants)
                self.css = s
            else:
                xp = s

            self.xpath = xp
            self.nsmap.update(XPATH_NAMESPACES)
            return ET.XPath ( xp, namespaces = self.nsmap )

        except Exception as e:
            return sublime.status_message(repr(e))

    def run(self, edit, lang='css', search_in_selections=False):
        view                  = self.view
        start_sels            = list(view.sel())
        sel_set               = view.sel()
        css_select            = lang == 'css'

        node_sels, node_proxy = selection_nodes(view, start_sels)
        if node_proxy is None: return

        self.nsmap = dict((k,v) for k,v in node_proxy.root
                                                     .nsmap
                                                     .copy().items() if k)

        def set_selections(sels):

            sels_list = list([r.begin(), r.end()] for r in sels)


            view.run_command('node_select_regions', dict(
                show_surrounds=False,
                regions=sels_list))

        restore_sels = partial(set_selections, start_sels)

        def on_something(s):
            if s == '': return restore_sels()

            nodes = []
            sel_set.clear()
            xselect = self.create_xpath(s, css_select, search_in_selections)
            if xselect is None: return restore_sels()

            if not search_in_selections:
                paths = xselect(node_proxy.root)
            else:
                paths = []
                for i, node in node_sels:
                    paths.extend(xselect(node_proxy.tags_lookup[i]) )

            for p in paths:
                nodes.extend(xp_2_selections(view, node_proxy, xselect.path, p))

            ############################################################

            set_selections(nodes if nodes else start_sels)
            sublime.status_message("%s (selected %s nodes)" % ( xselect.path,
                                                               len(nodes)) )

        last_search = ( (lang == 'css' and self.css) or self.xpath ) or ''

        panel = view.window().show_input_panel (
                'Enter %s Selector: ' % lang,
                last_search, on_something, on_something,
                restore_sels )

        self.configure_panel(panel, lang)

    def configure_panel(self, panel, lang):
        if lang == 'css':
            panel.assign_syntax('Packages/CSS/CSS.tmLanguage')
            panel.settings().set('auto_complete', False)
            panel.settings().set('gutter', False)
            panel.settings().set("line_numbers", False)

############################# NODE SELECT COMMANDS #############################

def node_select_cmd(clear_sels=True):
    def wrapper(f):
        def wrapped(self, edit, **args):
            view = self.view
            nodes, node_proxy = selection_nodes(view)

            if node_proxy is not None and nodes:
                nodes = reversed(nodes)
                start_sels = list(view.sel())
                if clear_sels: view.sel().clear()

                for region in f(self, view, start_sels, nodes,
                                      node_proxy, **args):
                    view.sel().add(region)
                    view.show(region)
        return wrapped
    return wrapper

class SelectNode(sublime_plugin.TextCommand):
    @node_select_cmd()
    def run(self, view, start_sels, nodes, node_proxy, xpath=None,
                                           selection_style="opening_tags"):

        for i, node in nodes:
            if xpath:
                els = node_proxy.tags_lookup[i].xpath(xpath)
                for e in [ xp_2_selections(view, node_proxy, xpath, e,
                           full=selection_style=="full_node")
                           for e in els ]:
                    yield from e
            else:
                yield node

        if not len(view.sel()):
            view.sel().add_all(start_sels)

class SelectElementName(sublime_plugin.TextCommand):
    @node_select_cmd()
    def run(self, view, start_sels, nodes, node_proxy, **args):
        for i, node in nodes:
            yield from element_name_regions(view, node)

class SelectInsideTag(sublime_plugin.TextCommand):
    @node_select_cmd()
    def run(self, view, start_sels, nodes, node_proxy, **args):
        for i, node in nodes:
            yield shrink_wrap_region (
                    view, sublime.Region(node.starts.end(), node.ends.begin()))

class MoveToNodeEnd(sublime_plugin.TextCommand):
    @node_select_cmd()
    def run(self, view, start_sels, nodes, node_proxy, **args):
        for sel, (i, node) in zip(reversed(start_sels), nodes):
            pt = sel.b
            ends = [node.a, node.b]

            if pt in ends:
                pt = ends[ ends.index(pt) -1 ]
            else:
                if len(start_sels) > 1:
                    pt = node.end()
                else:
                    pt = min (
                            (abs(node.begin() - pt), node.end()),
                            (abs(node.end()   - pt), node.begin()) ) [1]

            yield sublime.Region(pt, pt)