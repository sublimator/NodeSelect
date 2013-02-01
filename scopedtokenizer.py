#coding: utf8
#################################### IMPORTS ###################################

# Std Libs
import re

# Sublime Libs
import sublime

################################### CONSTANTS ##################################

TAG = re.compile(r"<\?.*?\?>|<!\s*?--.*?-->|<[^>]+>", re.M | re.S )
PHP_SHORT_TAG = re.compile('^'+ re.escape('<?=') + r'(\s*)')

################################################################################

def crude_tokenizer(text): # TODO: this would be a better algorithm for `inversion_stream`
    "Yields (token, start, end)"

    last_end = end =  0

    for match in TAG.finditer(text):
        start, end = match.span()

        if start != last_end:
            yield text[last_end:start], last_end, start

        yield text[start:end], start, end
        last_end = end

    token_length    = len(text)

    if end < token_length:
        yield text[end:token_length], end, token_length

def find_with_scope(view, pattern, scope, start_pos=0, cond=True, flags=0):

    max_pos = view.size()

    while start_pos < max_pos:
        f = view.find(pattern, start_pos, flags )

        if not f or view.match_selector( f.begin(), scope) is cond:
            break
        else:
            start_pos = f.end()

    return f

def catch_up_to(to, tokenizer):
    end = None

    while end is None or end < to:
        token, start, end = next(tokenizer)

    yield token[to-start:], to, end

def escape_php_token(t):
    #arst
    t = re.sub('<\?', '&lt;?', t)
    return re.sub('\?>', '?&gt;', t)

def handle_short_tags(token):
    return PHP_SHORT_TAG.sub(lambda m: '<?phpshort ' + m.group(1), token)

def scoped_tokenizer(view, tokenizer):
    "Messy but it's peformant"

    while True:
        token, start, end = next(tokenizer)

        if token.endswith('?>'):
            if view.match_selector(end-1, 'string'):
                f = find_with_scope( view,  '>', 'string', end, False)

                if f:
                    end = f.end()
                    t_region = sublime.Region(start, end)

                    normed_token = handle_short_tags(view.substr(t_region))

                    if not token.startswith('<?'):
                        normed_token = escape_php_token (normed_token)

                    yield normed_token, start, f.end()

                    for c in catch_up_to(end, tokenizer):
                        yield c

                    continue

            token = handle_short_tags(token)
        yield token, start, end