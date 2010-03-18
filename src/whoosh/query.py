#===============================================================================
# Copyright 2007 Matt Chaput
# 
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
#    http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#===============================================================================

"""This module contains objects that query the search index. These query
objects are composable to form complex query trees.
"""

from __future__ import division

__all__ = ("QueryError", "Term", "And", "Or", "Not", "DisjunctionMax",
           "Prefix", "Wildcard", "FuzzyTerm", "TermRange", "Variations",
           "Phrase", "NullQuery", "Require", "AndMaybe", "AndNot")

import copy
from bisect import bisect_left, bisect_right
import fnmatch, re

from whoosh.lang.morph_en import variations
from whoosh.matching import *
from whoosh.reading import TermNotFound
from whoosh.support.bitvector import BitVector
from whoosh.support.levenshtein import relative

# Utilities

def _not_vector(searcher, notqueries, sourcevector):
    # Returns a BitVector where the positions are docnums
    # and True means the docnum is banned from the results.
    # 'sourcevector' is the incoming exclude_docs. This
    # function makes a copy of it and adds the documents
    # from notqueries

    if sourcevector is None:
        nvector = BitVector(searcher.reader().doc_count_all())
    else:
        nvector = sourcevector.copy()

    for nquery in notqueries:
        nvector.set_from(nquery.docs(searcher))

    return nvector


# Exceptions

class QueryError(Exception):
    """Error encountered while running a query.
    """
    pass


# Base classes

class Query(object):
    """Abstract base class for all queries.
    
    Note that this base class implements __or__, __and__, and __sub__ to allow
    slightly more convenient composition of query objects::
    
        >>> Term("content", u"a") | Term("content", u"b")
        Or([Term("content", u"a"), Term("content", u"b")])
        
        >>> Term("content", u"a") & Term("content", u"b")
        And([Term("content", u"a"), Term("content", u"b")])
        
        >>> Term("content", u"a") - Term("content", u"b")
        And([Term("content", u"a"), Not(Term("content", u"b"))])
    """

    def __or__(self, query):
        """Allows you to use | between query objects to wrap them in an Or
        query.
        """
        return Or([self, query]).normalize()

    def __and__(self, query):
        """Allows you to use & between query objects to wrap them in an And
        query.
        """
        return And([self, query]).normalize()

    def __sub__(self, query):
        """Allows you to use - between query objects to add the right-hand
        query as a "NOT" query.
        """

        return And([self, Not(query)]).normalize()

    def all_terms(self, termset=None, phrases=True):
        """Returns a set of all terms in this query tree.
        
        This method simply operates on the query itself, without reference to
        an index (unlike existing_terms()), so it will *not* add terms that
        require an index to compute, such as Prefix and Wildcard.
        
        >>> q = And([Term("content", u"render"), Term("path", u"/a/b")])
        >>> q.all_terms()
        set([("content", u"render"), ("path", u"/a/b")])
        
        :param phrases: Whether to add words found in Phrase queries.
        :rtype: set
        """

        if termset is None:
            termset = set()
        self._all_terms(termset, phrases=phrases)
        return termset

    def existing_terms(self, ixreader, termset=None, reverse=False,
                       phrases=True):
        """Returns a set of all terms in this query tree that exist in the
        index represented by the given ixreaderder.
        
        This method references the IndexReader to expand Prefix and Wildcard
        queries, and only adds terms that actually exist in the index (unless
        reverse=True).
        
        >>> ixreader = my_index.reader()
        >>> q = And([Or([Term("content", u"render"),
        ...             Term("content", u"rendering")]),
        ...             Prefix("path", u"/a/")])
        >>> q.existing_terms(ixreader, termset)
        set([("content", u"render"), ("path", u"/a/b"), ("path", u"/a/c")])
        
        :param ixreader: A :class:`whoosh.reading.IndexReader` object.
        :param reverse: If True, this method adds *missing* terms rather than
            *existing* terms to the set.
        :param phrases: Whether to add words found in Phrase queries.
        :rtype: set
        """

        if termset is None:
            termset = set()
        self._existing_terms(ixreader, termset, reverse=reverse,
                             phrases=phrases)
        return termset

    def estimate_size(self, ixreader):
        """Returns an estimate of how many documents this query could
        potentially match (for example, the estimated size of a simple term
        query is the document frequency of the term). It is permissible to
        overestimate, but not to underestimate.
        """
        raise NotImplementedError

    def matcher(self, searcher, exclude_docs=None):
        """Returns a :class:`~whoosh.matching.Matcher` object you can use to
        retrieve documents and scores matching this query.
        
        :rtype: :class:`whoosh.matching.Matcher`
        """
        raise NotImplementedError

    def docs(self, searcher, exclude_docs=None):
        """Returns an iterator of docnums matching this query.
        
        >>> searcher = my_index.searcher()
        >>> list(my_query.docs(searcher))
        [10, 34, 78, 103]
        
        :param searcher: A :class:`whoosh.searching.Searcher` object.
        :param exclude_docs: A :class:`~whoosh.support.bitvector.BitVector`
            of document numbers to exclude from the results, or None to not
            exclude any documents.
        """

        try:
            return self.matcher(searcher, exclude_docs=exclude_docs).all_ids()
        except TermNotFound:
            return []

    def normalize(self):
        """Returns a recursively "normalized" form of this query. The
        normalized form removes redundancy and empty queries. This is called
        automatically on query trees created by the query parser, but you may
        want to call it yourself if you're writing your own parser or building
        your own queries.
        
        >>> q = And([And([Term("f", u"a"),
        ...               Term("f", u"b")]),
        ...               Term("f", u"c"), Or([])])
        >>> q.normalize()
        And([Term("f", u"a"), Term("f", u"b"), Term("f", u"c")])
        
        Note that this returns a *new, normalized* query. It *does not* modify
        the original query "in place".
        """
        return self

    def simplify(self, ixreader):
        """Returns a recursively simplified form of this query, where
        "second-order" queries (such as Prefix and Variations) are re-written
        into lower-level queries (such as Term and Or).
        """
        return self

    def replace(self, oldtext, newtext):
        """Returns a copy of this query with oldtext replaced by newtext (if
        oldtext was anywhere in this query).
        
        Note that this returns a *new* query with the given text replaced. It
        *does not* modify the original query "in place".
        """
        return self

    def accept(self, visitor):
        """Accepts a "visitor" function, applies it to any sub-queries and then
        to this object itself, and returns the result.
        """

        return visitor(copy.deepcopy(self))


class CompoundQuery(Query):
    """Abstract base class for queries that combine or manipulate the results
    of multiple sub-queries .
    """

    def __init__(self, subqueries, boost=1.0):
        self.subqueries = subqueries
        self._notqueries = None
        self.boost = boost

    def __repr__(self):
        r = "%s(%r" % (self.__class__.__name__, self.subqueries)
        if self.boost != 1:
            r += ", boost=%s" % self.boost
        r += ")"
        return r

    def __unicode__(self):
        r = u"("
        r += (self.JOINT).join([unicode(s) for s in self.subqueries])
        r += u")"
        return r

    def __eq__(self, other):
        return other and self.__class__ is other.__class__ and\
        self.subqueries == other.subqueries and\
        self.boost == other.boost

    def __getitem__(self, i):
        return self.subqueries.__getitem__(i)

    def replace(self, oldtext, newtext):
        return self.__class__([q.replace(oldtext, newtext)
                               for q in self.subqueries], boost=self.boost)

    def accept(self, visitor):
        qs = [q.accept(visitor) for q in self.subqueries]
        return visitor(self.__class__(qs, boost=self.boost))

    def _all_terms(self, termset, phrases=True):
        for q in self.subqueries:
            q.all_terms(termset, phrases=phrases)

    def _existing_terms(self, ixreader, termset, reverse=False, phrases=True):
        for q in self.subqueries:
            q.existing_terms(ixreader, termset, reverse=reverse,
                             phrases=phrases)

    def normalize(self):
        # Do an initial check for NullQuery.
        subqueries = [q for q in self.subqueries if q is not NullQuery]

        if not subqueries:
            return NullQuery

        # Normalize the subqueries and eliminate duplicate terms.
        subqs = []
        seenterms = set()
        for s in subqueries:
            s = s.normalize()
            if s is NullQuery:
                continue

            if isinstance(s, Term):
                term = (s.fieldname, s.text)
                if term in seenterms:
                    continue
                seenterms.add(term)

            if isinstance(s, self.__class__):
                subqs += s.subqueries
            else:
                subqs.append(s)

        if not subqs:
            return NullQuery
        if len(subqs) == 1:
            return subqs[0]

        return self.__class__(subqs, boost=self.boost)

    def _split_queries(self):
        subs = [q for q in self.subqueries if not isinstance(q, Not)]
        nots = [q.query for q in self.subqueries if isinstance(q, Not)]
        return (subs, nots)

    def simplify(self, ixreader):
        subs, nots = self._split_queries()

        if subs:
            subs = self.__class__([subq.simplify(ixreader) for subq in subs],
                                  boost=self.boost)
            if nots:
                nots = Or(nots).normalize().simplify()
                return AndNot(subs, nots)
            else:
                return subs
        else:
            return NullQuery

    def _submatchers(self, searcher, exclude_docs):
        subs, nots = self._split_queries()
        exclude_docs = _not_vector(searcher, nots, exclude_docs)
        subs.sort(key=lambda q: q.estimate_size(searcher))
        
        return [subquery.matcher(searcher, exclude_docs=exclude_docs)
                for subquery in subs]

    def _matcher(self, matchercls, searcher, exclude_docs):
        submatchers = self._submatchers(searcher, exclude_docs)
        
        tree = make_tree(matchercls, submatchers)
        if self.boost == 1.0:
            return tree
        else:
            return WrappingMatcher(tree, self.boost)


class MultiTerm(Query):
    """Abstract base class for queries that operate on multiple terms in the
    same field.
    """

    def _words(self, ixreader):
        raise NotImplementedError

    def simplify(self, ixreader):
        return Or([Term(self.fieldname, word, boost=self.boost)
                   for word in self._words(ixreader)])

    def _all_terms(self, termset, phrases=True):
        pass

    def _existing_terms(self, ixreader, termset, reverse=False, phrases=True):
        fieldname = self.fieldname
        for word in self._words(ixreader):
            t = (fieldname, word)
            contains = t in ixreader
            if reverse: contains = not contains
            if contains:
                termset.add(t)

    def estimate_size(self, ixreader):
        fieldnum = ixreader.fieldname_to_num(self.fieldname)
        return sum(ixreader.doc_frequency(fieldnum, text)
                   for text in self._words(ixreader))

    def matcher(self, searcher, exclude_docs=None):
        matchers = []
        fieldname = self.fieldname
        for word in self._words(searcher.reader()):
            try:
                q = Term(fieldname, word).matcher(searcher,
                                                  exclude_docs=exclude_docs)
                matchers.append(q)
            except TermNotFound:
                pass

        if matchers:
            return Or(matchers, boost=self.boost).matcher(searcher)
        else:
            return NullMatcher()


# Concrete classes

class Term(Query):
    """Matches documents containing the given term (fieldname+text pair).
    
    >>> Term("content", u"render")
    """

    __inittypes__ = dict(fieldname=str, text=unicode, boost=float)

    def __init__(self, fieldname, text, boost=1.0):
        self.fieldname = fieldname
        self.text = text
        self.boost = boost

    def __eq__(self, other):
        return (other
                and self.__class__ is other.__class__
                and
                self.fieldname == other.fieldname
                and self.text == other.text
                and self.boost == other.boost)

    def __repr__(self):
        r = "%s(%r, %r" % (self.__class__.__name__, self.fieldname, self.text)
        if self.boost != 1:
            r += ", boost=%s" % self.boost
        r += ")"
        return r

    def __unicode__(self):
        t = u"%s:%s" % (self.fieldname, self.text)
        if self.boost != 1:
            t += u"^" + unicode(self.boost)
        return t

    def _all_terms(self, termset, phrases=True):
        termset.add((self.fieldname, self.text))

    def _existing_terms(self, ixreader, termset, reverse=False, phrases=True):
        fieldname, text = self.fieldname, self.text
        fieldnum = ixreader.fieldname_to_num(fieldname)
        contains = (fieldnum, text) in ixreader
        if reverse: contains = not contains
        if contains:
            termset.add((fieldname, text))

    def replace(self, oldtext, newtext):
        if self.text == oldtext:
            return Term(self.fieldname, newtext, boost=self.boost)
        else:
            return self

    def estimate_size(self, ixreader):
        fieldnum = ixreader.fieldname_to_num(self.fieldname)
        return ixreader.doc_frequency(fieldnum, self.text)

    def matcher(self, searcher, exclude_docs=None):
        fieldnum = searcher.fieldname_to_num(self.fieldname)
        try:
            return searcher.postings(fieldnum, self.text,
                                     exclude_docs=exclude_docs)
        except TermNotFound:
            return NullMatcher()
        

class And(CompoundQuery):
    """Matches documents that match ALL of the subqueries.
    
    >>> And([Term("content", u"render"),
    ...      Term("content", u"shade"),
    ...      Not(Term("content", u"texture"))])
    >>> # You can also do this
    >>> Term("content", u"render") & Term("content", u"shade")
    """

    # This is used by the superclass's __unicode__ method.
    JOINT = " AND "

    def estimate_size(self, ixreader):
        return min(q.estimate_size(ixreader) for q in self.subqueries)

    def matcher(self, searcher, exclude_docs=None):
        return self._matcher(IntersectionMatcher, searcher, exclude_docs)


class Or(CompoundQuery):
    """Matches documents that match ANY of the subqueries.
    
    >>> Or([Term("content", u"render"),
    ...     And([Term("content", u"shade"), Term("content", u"texture")]),
    ...     Not(Term("content", u"network"))])
    >>> # You can also do this
    >>> Term("content", u"render") | Term("content", u"shade")
    """

    # This is used by the superclass's __unicode__ method.
    JOINT = " OR "

    def __init__(self, subqueries, boost=1.0, minmatch=0):
        CompoundQuery.__init__(self, subqueries, boost=boost)
        self.minmatch = minmatch

    def __repr__(self):
        r = "%s(%r" % (self.__class__.__name__, self.subqueries)
        if self.boost != 1:
            r += ", boost=%s" % self.boost
        if self.minmatch:
            r += ", minmatch=%s" % self.minmatch
        r += ")"
        return r

    def __unicode__(self):
        r = u"("
        r += (self.JOINT).join([unicode(s) for s in self.subqueries])
        r += u")"
        if self.minmatch:
            r += u">%s" % self.minmatch
        return r

    def estimate_size(self, ixreader):
        return sum(q.estimate_size(ixreader) for q in self.subqueries)
    
    def normalize(self):
        norm = CompoundQuery.normalize(self)
        if norm.__class__ is self.__class__:
            norm.minmatch = self.minmatch
        return norm

    def matcher(self, searcher, exclude_docs=None):
        return self._matcher(UnionMatcher, searcher, exclude_docs)


class DisjunctionMax(CompoundQuery):
    """Matches all documents that match any of the subqueries, but scores each
    document using the maximum score from the subqueries.
    """

    def __init__(self, subqueries, boost=1.0, tiebreak=0.0):
        CompoundQuery.__init__(self, subqueries, boost=boost)
        self.tiebreak = tiebreak

    def __unicode__(self):
        s = u"DisMax" + Or.__unicode__(self)
        if self.tiebreak:
            s += u"~" + unicode(self.tiebreak)
        return s

    def estimate_size(self, ixreader):
        return Or.estimate_size(self, ixreader)

    def normalize(self):
        norm = CompoundQuery.normalize(self)
        if norm.__class__ is self.__class__:
            norm.tiebreak = self.tiebreak
        return norm
    
    def matcher(self, searcher, exclude_docs=None):
        return self._matcher(DisjunctionMaxMatcher, searcher, exclude_docs)


class Not(Query):
    """Excludes any documents that match the subquery.
    
    >>> # Match documents that contain 'render' but not 'texture'
    >>> And([Term("content", u"render"),
    ...      Not(Term("content", u"texture"))])
    >>> # You can also do this
    >>> Term("content", u"render") - Term("content", u"texture")
    """

    __inittypes__ = dict(query=Query)

    def __init__(self, query, boost=1.0):
        """
        :param query: A :class:`Query` object. The results of this query
            are *excluded* from the parent query.
        :param boost: Boost is meaningless for excluded documents but this
            keyword argument is accepted for the sake of a consistent interface.
        """

        self.query = query
        self.boost = boost

    def __eq__(self, other):
        return other and self.__class__ is other.__class__ and\
        self.query == other.query

    def __repr__(self):
        return "%s(%s)" % (self.__class__.__name__, repr(self.query))

    def __unicode__(self):
        return u"NOT " + unicode(self.query)

    def normalize(self):
        query = self.query.normalize()
        if query is NullQuery:
            return NullQuery
        else:
            return self.__class__(query, boost=self.boost)

    def replace(self, oldtext, newtext):
        return Not(self.query.replace(oldtext, newtext), boost=self.boost)

    def accept(self, visitor):
        return visitor(Not(self.query.accept(visitor), boost=self.boost))

    def _all_terms(self, termset, phrases=True):
        self.query.all_terms(termset, phrases=phrases)

    def _existing_terms(self, ixreader, termset, reverse=False, phrases=True):
        self.query.existing_terms(ixreader, termset, reverse=reverse,
                                  phrases=phrases)

    def estimate_size(self, ixreader):
        return ixreader.doc_count()

    def matcher(self, searcher, exclude_docs=None):
        reader = searcher.reader()
        child = self.query.matcher(searcher)
        return InverseMatcher(child, reader.doc_count_all(),
                              missing=reader.is_deleted)


class Prefix(MultiTerm):
    """Matches documents that contain any terms that start with the given text.
    
    >>> # Match documents containing words starting with 'comp'
    >>> Prefix("content", u"comp")
    """

    __inittypes__ = dict(fieldname=str, text=unicode, boost=float)

    def __init__(self, fieldname, text, boost=1.0):
        self.fieldname = fieldname
        self.text = text
        self.boost = boost

    def __eq__(self, other):
        return other and self.__class__ is other.__class__ and\
        self.fieldname == other.fieldname and self.text == other.text and\
        self.boost == other.boost

    def __repr__(self):
        r = "%s(%r, %r" % (self.__class__.__name__, self.fieldname, self.text)
        if self.boost != 1:
            r += ", boost=" + self.boost
        r += ")"
        return r

    def __unicode__(self):
        return "%s:%s*" % (self.fieldname, self.text)

    def _words(self, ixreader):
        return ixreader.expand_prefix(self.fieldname, self.text)


_wildcard_exp = re.compile("(.*?)([?*]|$)");
class Wildcard(MultiTerm):
    """Matches documents that contain any terms that match a wildcard
    expression.
    
    >>> Wildcard("content", u"in*f?x")
    """

    __inittypes__ = dict(fieldname=str, text=unicode, boost=float)

    def __init__(self, fieldname, text, boost=1.0):
        """
        :param fieldname: The field to search in.
        :param text: A glob to search for. May contain ? and/or * wildcard
            characters. Note that matching a wildcard expression that starts
            with a wildcard is very inefficent, since the query must test every
            term in the field.
        :param boost: A boost factor that should be applied to the raw score of
            results matched by this query.
        """

        self.fieldname = fieldname
        self.text = text
        self.boost = boost

        self.expression = re.compile(fnmatch.translate(text))

        # Get the "prefix" -- the substring before the first wildcard.
        qm = text.find("?")
        st = text.find("*")
        if qm < 0 and st < 0:
            self.prefix = ""
        elif qm < 0:
            self.prefix = text[:st]
        elif st < 0:
            self.prefix = text[:qm]
        else:
            self.prefix = text[:min(st, qm)]

    def __eq__(self, other):
        return other and self.__class__ is other.__class__ and\
        self.fieldname == other.fieldname and self.text == other.text and\
        self.boost == other.boost

    def __repr__(self):
        r = "%s(%r, %r" % (self.__class__.__name__, self.fieldname, self.text)
        if self.boost != 1:
            r += ", boost=%s" % self.boost
        r += ")"
        return r

    def __unicode__(self):
        return "%s:%s" % (self.fieldname, self.text)

    def _words(self, ixreader):
        if self.prefix:
            candidates = ixreader.expand_prefix(self.fieldname, self.prefix)
        else:
            candidates = ixreader.lexicon(self.fieldname)

        exp = self.expression
        for text in candidates:
            if exp.match(text):
                yield text

    def normalize(self):
        # If there are no wildcard characters in this "wildcard", turn it into
        # a simple Term.
        text = self.text
        if text == "*":
            return Every(boost=self.boost)
        if "*" not in text and "?" not in text:
            # If no wildcard chars, convert to a normal term.
            return Term(self.fieldname, self.text, boost=self.boost)
        elif ("?" not in text
              and text.endswith("*")
              and text.find("*") == len(text) - 1
              and (len(text) < 2 or text[-2] != "\\")):
            # If the only wildcard char is an asterisk at the end, convert to a
            # Prefix query.
            return Prefix(self.fieldname, self.text[:-1], boost=self.boost)
        else:
            return self


class FuzzyTerm(MultiTerm):
    """Matches documents containing words similar to the given term.
    """

    __inittypes__ = dict(fieldname=str, text=unicode, boost=float,
                         minsimilarity=float, prefixlength=int)

    def __init__(self, fieldname, text, boost=1.0, minsimilarity=0.5,
                 prefixlength=1):
        """
        :param fieldname: The name of the field to search.
        :param text: The text to search for.
        :param boost: A boost factor to apply to scores of documents matching
            this query.
        :param minsimilarity: The minimum similarity ratio to match. 1.0 is the
            maximum (an exact match to 'text').
        :param prefixlength: The matched terms must share this many initial
            characters with 'text'. For example, if text is "light" and
            prefixlength is 2, then only terms starting with "li" are checked
            for similarity.
        """

        if not text:
            raise QueryError("Fuzzy term is empty")

        self.fieldname = fieldname
        self.text = text
        self.boost = boost
        self.minsimilarity = minsimilarity
        self.prefixlength = prefixlength

    def __eq__(self, other):
        return (other
                and self.__class__ is other.__class__
                and self.fieldname == other.fieldname
                and self.text == other.text
                and self.minsimilarity == other.minsimilarity
                and self.prefixlength == other.prefixlength
                and self.boost == other.boost)

    def __repr__(self):
        return "%s(%r, %r, ratio=%f)" % (self.__class__.__name__,
                                         self.fieldname, self.text,
                                         self.ratio)

    def __unicode__(self):
        return u"~" + self.text

    def _all_terms(self, termset, phrases=True):
        termset.add((self.fieldname, self.text))

    def _words(self, ixreader):
        text = self.text
        minsim = self.minsimilarity
        for term in ixreader.expand_prefix(self.fieldname,
                                           text[:self.prefixlength]):
            if text == term:
                yield term
            elif relative(text, term) > minsim:
                yield term


class TermRange(MultiTerm):
    """Matches documents containing any terms in a given range.
    
    >>> # Match documents where the indexed "id" field is greater than or equal
    >>> # to 'apple' and less than or equal to 'pear'.
    >>> TermRange("id", u"apple", u"pear")
    """

    def __init__(self, fieldname, start, end, startexcl=False, endexcl=False,
                 boost=1.0):
        """
        :param fieldname: The name of the field to search.
        :param start: Match terms equal to or greather than this.
        :param end: Match terms equal to or less than this.
        :param startexcl: If True, the range start is exclusive. If False, the
            range start is inclusive.
        :param endexcl: If True, the range end is exclusive. If False, the
            range end is inclusive.
        :param boost: Boost factor that should be applied to the raw score of
            results matched by this query.
        """

        self.fieldname = fieldname
        self.start = start
        self.end = end
        self.startexcl = startexcl
        self.endexcl = endexcl
        self.boost = boost

    def __eq__(self, other):
        return (other
                and self.__class__ is other.__class__
                and self.fieldname == other.fieldname
                and self.start == other.start
                and self.end == other.end
                and self.startexcl == other.startexcl
                and self.endexcl == other.endexcl
                and self.boost == other.boost)

    def __repr__(self):
        return '%s(%r, %r, %r, %s, %s)' % (self.__class__.__name__,
                                           self.fieldname,
                                           self.start, self.end,
                                           self.startexcl, self.endexcl)

    def __unicode__(self):
        startchar = "["
        if self.startexcl: startchar = "{"
        endchar = "]"
        if self.endexcl: endchar = "}"
        return u"%s:%s%s TO %s%s" % (self.fieldname,
                                     startchar, self.start, self.end, endchar)

    def normalize(self):
        if self.start == self.end:
            return Term(self.fieldname, self.start, boost=self.boost)
        else:
            return TermRange(self.fieldname, self.start, self.end,
                             self.startexcl, self.endexcl,
                             boost=self.boost)

    def replace(self, oldtext, newtext):
        if self.start == oldtext:
            return TermRange(self.fieldname, newtext, self.end,
                             self.startexcl, self.endexcl, boost=self.boost)
        elif self.end == oldtext:
            return TermRange(self.fieldname, self.start, newtext,
                             self.startexcl, self.endexcl, boost=self.boost)
        else:
            return self

    def _words(self, ixreader):
        fieldnum = ixreader.fieldname_to_num(self.fieldname)
        start = self.start
        end = self.end
        startexcl = self.startexcl
        endexcl = self.endexcl

        for fnum, t, _, _ in ixreader.iter_from(fieldnum, self.start):
            if fnum != fieldnum:
                break
            if t == start and startexcl:
                continue
            if t == end and endexcl:
                break
            if t > end:
                break
            yield t


class Variations(MultiTerm):
    """Query that automatically searches for morphological variations of the
    given word in the same field.
    """

    def __init__(self, fieldname, text, boost=1.0):
        self.fieldname = fieldname
        self.text = text
        self.boost = boost
        self.words = variations(self.text)

    def __repr__(self):
        r = "%s(%r, %r" % (self.__class__.__name__, self.fieldname, self.text)
        if self.boost != 1:
            r += ", boost=%s" % self.boost
        r += ")"
        return r

    def __eq__(self, other):
        return other and self.__class__ is other.__class__ and\
        self.fieldname == other.fieldname and self.text == other.text and\
        self.boost == other.boost

    def _all_terms(self, termset, phrases=True):
        termset.add(self.text)

    def _words(self, ixreader):
        fieldname = self.fieldname
        return [word for word in self.words if (fieldname, word) in ixreader]

    def __unicode__(self):
        return u"%s:<%s>" % (self.fieldname, self.text)

    def replace(self, oldtext, newtext):
        if oldtext == self.text:
            return Variations(self.fieldname, newtext, boost=self.boost)
        else:
            return self


class Phrase(MultiTerm):
    """Matches documents containing a given phrase."""

    def __init__(self, fieldname, words, slop=1, boost=1.0):
        """
        :param fieldname: the field to search.
        :param words: a list of words (unicode strings) in the phrase.
        :param slop: the number of words allowed between each "word" in the
            phrase; the default of 1 means the phrase must match exactly.
        :param boost: a boost factor that to apply to the raw score of
            documents matched by this query.
        """

        self.fieldname = fieldname
        self.words = words
        self.slop = slop
        self.boost = boost

    def __eq__(self, other):
        return other and self.__class__ is other.__class__ and\
        self.fieldname == other.fieldname and self.words == other.word and\
        self.slop == other.slop and self.boost == other.boost

    def __repr__(self):
        return "%s(%r, %r, slop=%s, boost=%f)" % (self.__class__.__name__,
                                                  self.fieldname, self.words,
                                                  self.slop, self.boost)

    def __unicode__(self):
        return u'%s:"%s"' % (self.fieldname, u" ".join(self.words))

    def _all_terms(self, termset, phrases=True):
        if phrases:
            fieldname = self.fieldname
            for word in self.words:
                termset.add((fieldname, word))

    def _existing_terms(self, ixreader, termset, reverse=False, phrases=True):
        if phrases:
            fieldname = self.fieldname
            fieldnum = ixreader.fieldname_to_num(fieldname)
            for word in self.words:
                contains = (fieldnum, word) in ixreader
                if reverse: contains = not contains
                if contains:
                    termset.add((fieldname, word))

    def normalize(self):
        if not self.words:
            return NullQuery
        if len(self.words) == 1:
            return Term(self.fieldname, self.words[0])

        words = [w for w in self.words if w is not None]
        return self.__class__(self.fieldname, words, slop=self.slop,
                              boost=self.boost)

    def replace(self, oldtext, newtext):
        def rep(w):
            if w == oldtext:
                return newtext
            else:
                return w

        return Phrase(self.fieldname, [rep(w) for w in self.words],
                      slop=self.slop, boost=self.boost)

    def _and_query(self):
        fn = self.fieldname
        return And([Term(fn, word) for word in self.words])

    def estimate_size(self, ixreader):
        return self._and_query().estimate_size(ixreader)

    def matcher(self, searcher, exclude_docs=None):
        fieldnum = searcher.fieldname_to_num(self.fieldname)

        # Shortcut the query if one of the words doesn't exist.
        for word in self.words:
            if (fieldnum, word) not in searcher: return NullMatcher()
        
        wordmatchers = [searcher.postings(fieldnum, word, exclude_docs=exclude_docs)
                        for word in self.words]
        isect = make_tree(wordmatchers)
        
        field = searcher.field(fieldnum)
        if field.format and field.format.supports("positions"):
            return PostingPhraseMatcher(wordmatchers, isect, slop=self.slop)
        
        elif field.vector and field.vector.supports("positions"):
            return VectorPhraseMatcher(searcher, fieldnum, self.words, isect,
                                       slop=self.slop)
            
        else:
            raise QueryError("Phrase search: %r field has no positions"
                             % self.fieldname)


class Every(Query):
    """A query that matches every document in the index.
    """

    def __eq__(self, other):
        return other and self.__class__ is other.__class__ and\
        self.boost == other.boost

    def __unicode__(self):
        return u"*"

    def estimate_size(self, ixreader):
        return ixreader.doc_count()

    def matcher(self, searcher, exclude_docs=None):
        if not exclude_docs:
            exclude_docs = frozenset()
        return EveryMatcher(searcher.reader().doc_count_all(), exclude_docs)


class NullQuery(Query):
    "Represents a query that won't match anything."
    def __call__(self):
        return self
    def estimate_size(self, ixreader):
        return 0
    def normalize(self):
        return self
    def simplify(self, ixreader):
        return self
    def docs(self, searcher, exclude_docs=None):
        return []
    def matcher(self, searcher, exclude_docs=None):
        return NullMatcher()
NullQuery = NullQuery()


class Require(CompoundQuery):
    """Binary query returns results from the first query that also appear in
    the second query, but only uses the scores from the first query. This lets
    you filter results without affecting scores.
    """

    JOINT = " REQUIRE "

    def __init__(self, scoredquery, requiredquery, boost=1.0):
        """
        :param scoredquery: The query that is scored. Only documents that also
            appear in the second query ('requiredquery') are scored.
        :param requiredquery: Only documents that match both 'scoredquery' and
            'requiredquery' are returned, but this query does not
            contribute to the scoring.
        """

        # The superclass CompoundQuery expects the subqueries to be in a
        # sequence in self.subqueries
        self.subqueries = (scoredquery, requiredquery)
        self.boost = boost

    def normalize(self):
        subqueries = [q.normalize() for q in self.subqueries]
        if NullQuery in subqueries:
            return NullQuery
        return Require(subqueries[0], subqueries[1], boost=self.boost)

    def docs(self, searcher, exclude_docs=None):
        return And(self.subqueries).docs(searcher, exclude_docs=exclude_docs)
    
    def matcher(self, searcher, exclude_docs=None):
        scored, required = self.subqueries
        return RequireMatcher(scored.matcher(searcher, exclude_docs=exclude_docs),
                              required.matcher(searcher, exclude_docs=exclude_docs))


class AndMaybe(CompoundQuery):
    """Binary query takes results from the first query. If and only if the
    same document also appears in the results from the second query, the score
    from the second query will be added to the score from the first query.
    """

    JOINT = " ANDMAYBE "

    def __init__(self, requiredquery, optionalquery, boost=1.0):
        """
        :param requiredquery: Documents matching this query are returned.
        :param optionalquery: If a document matches this query as well as
            'requiredquery', the score from this query is added to the
            document score from 'requiredquery'.
        """

        # The superclass CompoundQuery expects the subqueries to be
        # in a sequence in self.subqueries
        self.subqueries = (requiredquery, optionalquery)
        self.boost = boost

    def normalize(self):
        required, optional = (q.normalize() for q in self.subqueries)
        if required is NullQuery:
            return NullQuery
        if optional is NullQuery:
            return required
        return AndMaybe(required, optional, boost=self.boost)

    def docs(self, searcher, exclude_docs=None):
        return self.subqueries[0].docs(searcher, exclude_docs=exclude_docs)
    
    def matcher(self, searcher, exclude_docs=None):
        required, optional = self.subqueries
        return AndMaybeMatcher(required.matcher(searcher, exclude_docs=exclude_docs),
                                optional.matcher(searcher, exclude_docs=exclude_docs))


class AndNot(Query):
    """Binary boolean query of the form 'a ANDNOT b', where documents that
    match b are removed from the matches for a.
    """

    def __init__(self, positive, negative, boost=1.0):
        """
        :param positive: query to INCLUDE.
        :param negative: query whose matches should be EXCLUDED.
        :param boost: boost factor that should be applied to the raw score of
            results matched by this query.
        """

        self.positive = positive
        self.negative = negative
        self.boost = boost

    def __eq__(self, other):
        return (other
                and self.__class__ is other.__class__
                and self.positive == other.positive
                and self.negative == other.negative
                and self.boost == other.boost)

    def __repr__(self):
        return "%s(%r, %r)" % (self.__class__.__name__,
                               self.positive, self.negative)

    def __unicode__(self):
        return u"%s ANDNOT %s" % (self.postive, self.negative)

    def normalize(self):
        pos = self.positive.normalize()
        neg = self.negative.normalize()

        if pos is NullQuery:
            return NullQuery
        elif neg is NullQuery:
            return pos

        return AndNot(pos, neg, boost=self.boost)

    def replace(self, oldtext, newtext):
        return AndNot(self.positive.replace(oldtext, newtext),
                      self.negative.replace(oldtext, newtext),
                      boost=self.boost)

    def _all_terms(self, termset, phrases=True):
        self.positive.all_terms(termset, phrases=phrases)

    def _existing_terms(self, ixreader, termset, reverse=False, phrases=True):
        self.positive.existing_terms(ixreader, termset, reverse=reverse,
                                     phrases=phrases)

    def matcher(self, searcher, exclude_docs=None):
        notvector = _not_vector(searcher, [self.negative], exclude_docs)
        return self.positive.matcher(searcher, exclude_docs=notvector)


def BooleanQuery(required, should, prohibited):
    return AndNot(AndMaybe(And(required), Or(should)), Or(prohibited)).normalize()












