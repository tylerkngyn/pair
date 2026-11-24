"""Original concise prompts and practice fixtures for the NeetCode 150 catalog.

These are inspectable practice tests, not LeetCode's private judge data. Each
fixture is (positional arguments, expected result). Special adapters are in the
browser harness, shared with native-Python verification tests.
"""
EXERCISES = {}


def add(number, prompt, *cases, compare="exact", mode="call"):
    EXERCISES[number] = {"prompt": prompt, "cases": [{"args": args, "expected": expected} for args, expected in cases], "compare": compare, "mode": mode}


def linked(values, pos=-1):
    return {"linked": values, "pos": pos}


def tree(values):
    return {"tree": values}


def design(init, *calls):
    return {"init": init, "calls": [[name, args] for name, args in calls]}


add(217, "Return whether an integer array contains a repeated value.", ([[2, 5, 2]], True), ([[4, 1, 9]], False), ([[]], False), ([[0, 0]], True))
add(242, "Return whether two strings contain exactly the same characters with the same frequencies.", (["silent", "listen"], True), (["aab", "abb"], False), (["", ""], True))
add(1, "Return two distinct zero-based indices whose values sum to target. Exactly one pair exists; either index order is accepted.", ([[4, 8, 3], 7], [0, 2]), ([[3, 3], 6], [0, 1]), ([[-5, 2, 7], 2], [0, 2]), compare="indices")
add(49, "Group strings that are anagrams. Group and member order do not matter.", ([["tea", "eat", "bat"]], [["tea", "eat"], ["bat"]]), ([["", ""]], [["", ""]]), ([["a"]], [["a"]]), compare="groups")
add(347, "Return the k most frequent integers, in any order. Test inputs have an unambiguous top k.", ([[4, 4, 4, 2, 2, 1], 2], [4, 2]), ([[8], 1], [8]), ([[-1, -1, 3], 1], [-1]), compare="unordered")
add(238, "For each position return the product of all other elements. Avoid division and aim for linear time.", ([[2, 3, 4]], [12, 8, 6]), ([[0, 2, 3]], [6, 0, 0]), ([[0, 0]], [0, 0]), ([[-1, 2]], [2, -1]))
_board = [list(row) for row in ["53..7....", "6..195...", ".98....6.", "8...6...3", "4..8.3..1", "7...2...6", ".6....28.", "...419..5", "....8..79"]]
add(36, "Validate a partially filled 9×9 Sudoku. Digits cannot repeat in a row, column, or 3×3 box; '.' is empty.", ([_board], True), ([[list("55.......")] + [list(".........") for _ in range(8)]], False), ([[list(".........") for _ in range(9)]], True))
add(271, "Implement encode(strs) and decode(s) so any list of Unicode strings round-trips, including empty strings and delimiter characters.", ([["a#b", "", "猫"]], ["a#b", "", "猫"]), ([[]], []), ([["12#", "x"]], ["12#", "x"]), mode="strings_codec")
add(128, "Return the length of the longest consecutive integer sequence, regardless of input order. Aim for linear time.", ([[10, 3, 2, 1, 4]], 4), ([[]], 0), ([[1, 1, 2]], 2))
add(125, "Ignore non-alphanumeric characters and letter case; return whether the remaining string is a palindrome. Inputs use ASCII.", (["Never odd or even!"], True), (["hello"], False), (["..."], True))
add(167, "In a sorted array, return the one-based indices of the unique pair summing to target.", ([[1, 4, 7, 10], 11], [1, 4]), ([[-3, 0, 5], 2], [1, 3]), ([[2, 2], 4], [1, 2]), compare="indices")
add(15, "Return all unique value triplets that sum to zero. Triplet and result order do not matter.", ([[-2, 0, 1, 1, 2]], [[-2, 0, 2], [-2, 1, 1]]), ([[0, 0, 0, 0]], [[0, 0, 0]]), ([[1, 2]], []), compare="groups")
add(11, "Choose two vertical lines from height to maximize the water container area between them.", ([[2, 7, 3, 8, 4]], 14), ([[1, 1]], 1), ([[5, 4, 3, 2, 1]], 6))
add(42, "Return the total water trapped above unit-width bars after rainfall.", ([[3, 0, 2, 0, 4]], 7), ([[1, 2, 3]], 0), ([[2, 0, 2]], 2))
add(121, "Return the maximum profit from one buy followed by one sell. Return zero if no profitable trade exists.", ([[8, 2, 6, 1, 5]], 4), ([[5, 4, 3]], 0), ([[2]], 0))
add(3, "Return the length of the longest substring with no repeated character.", (["abcabcbb"], 3), ([""], 0), (["pwwkew"], 3), (["aaaa"], 1))
add(424, "Replace at most k uppercase letters to make a longest possible substring consist of one repeated letter. Return its length.", (["AABABBA", 1], 4), (["ABAB", 2], 4), (["ABCD", 0], 1))
add(567, "Return whether s2 contains a contiguous permutation of s1. Strings contain lowercase English letters.", (["ab", "eidbaooo"], True), (["ab", "eidboaoo"], False), (["abc", "cba"], True))
add(76, "Return the shortest substring of s containing every character of t with its required multiplicity, or an empty string. Nonempty answers in these tests are unique.", (["ADOBECODEBANC", "ABC"], "BANC"), (["a", "aa"], ""), (["aa", "aa"], "aa"))
add(239, "Return the maximum value in each contiguous window of k elements.", ([[2, 1, 5, 3, 4], 3], [5, 5, 5]), ([[1], 1], [1]), ([[-2, -1, -3], 2], [-1, -1]))
add(20, "Return whether a string of (), [], and {} brackets is correctly nested and closed.", (["([]){}"], True), (["([)]"], False), (["("], False), ([""], True))
add(155, "Implement MinStack: push, pop, top, and getMin. Each operation should take constant time. pop returns None.", ([design([], ("push", [-2]), ("push", [0]), ("push", [-3]), ("getMin", []), ("pop", []), ("top", []), ("getMin", []))], [None, None, None, -3, None, 0, -2]), ([design([], ("push", [2]), ("getMin", []), ("top", []))], [None, 2, 2]), mode="design")
add(150, "Evaluate reverse Polish notation tokens. Integer division truncates toward zero.", ([["2", "3", "+", "4", "*"]], 20), ([["7", "-3", "/"]], -2), ([["5"]], 5))
add(22, "Return every balanced parentheses string with n pairs, in any order.", ([1], ["()"]), ([3], ["((()))", "(()())", "(())()", "()(())", "()()()"]), ([2], ["(())", "()()"]), compare="unordered")
add(739, "For each daily temperature, return how many days until a strictly warmer day, or zero.", ([[70, 71, 69, 73]], [1, 2, 1, 0]), ([[50, 50]], [0, 0]), ([[90, 80, 70]], [0, 0, 0]))
add(853, "Cars move toward target at their given speeds without passing. Return the number of fleets arriving at target.", ([12, [10, 8, 0, 5, 3], [2, 4, 1, 1, 3]], 3), ([10, [3], [3]], 1), ([10, [0, 4], [4, 2]], 1))
add(84, "Return the largest rectangular area under a histogram of unit-width bars.", ([[2, 1, 5, 6, 2, 3]], 10), ([[2, 4]], 4), ([[0]], 0))
add(704, "Return the index of target in a strictly increasing array, or -1. Use logarithmic time.", ([[-5, 0, 3, 8], 3], 2), ([[1], 2], -1), ([[], 3], -1))
add(74, "Search a matrix whose rows are increasing and whose next row starts above the previous row's final value.", ([[[1, 3, 5], [7, 9, 11]], 9], True), ([[[1, 3], [5, 7]], 4], False), ([[[1]], 1], True))
add(875, "Return the smallest integer eating speed to finish all banana piles within h hours. Each hour handles one pile.", ([[3, 6, 7, 11], 8], 4), ([[30, 11, 23, 4, 20], 5], 30), ([[1], 2], 1))
add(153, "Return the minimum of a rotated strictly increasing array in logarithmic time.", ([[4, 5, 1, 2, 3]], 1), ([[1, 2, 3]], 1), ([[7]], 7))
add(33, "Return target's index in a rotated strictly increasing array, or -1, in logarithmic time.", ([[4, 5, 6, 0, 1, 2], 0], 3), ([[1], 0], -1), ([[3, 1], 1], 1))
add(981, "Implement TimeMap.set(key,value,timestamp) and get(key,timestamp). get returns the latest value at or before timestamp, or ''. Set timestamps increase.", ([design([], ("set", ["a", "one", 2]), ("get", ["a", 1]), ("get", ["a", 3]), ("set", ["a", "two", 5]), ("get", ["a", 5]))], [None, "", "one", None, "two"]), ([design([], ("get", ["missing", 3]))], [""]), mode="design")
add(4, "Return the median of two sorted arrays, with logarithmic-time selection as the target approach. At least one array is nonempty.", ([[1, 3], [2]], 2.0), ([[1, 2], [3, 4]], 2.5), ([[], [2]], 2.0))
add(206, "Reverse a singly linked list and return its new head. ListNode has val and next.", ([linked([1, 2, 3])], [3, 2, 1]), ([linked([])], []), ([linked([7])], [7]))
add(21, "Merge two sorted linked lists and return the sorted head.", ([linked([1, 3]), linked([2, 4])], [1, 2, 3, 4]), ([linked([]), linked([0])], [0]), ([linked([]), linked([])], []))
add(143, "Reorder a linked list in place as first, last, second, second-last, and so on. Do not change node values.", ([linked([1, 2, 3, 4])], [1, 4, 2, 3]), ([linked([1, 2, 3, 4, 5])], [1, 5, 2, 4, 3]), ([linked([1])], [1]), mode="inplace")
add(19, "Remove the nth node from the end of a linked list and return the new head; n is valid.", ([linked([1, 2, 3, 4]), 2], [1, 2, 4]), ([linked([1]), 1], []), ([linked([1, 2]), 2], [2]))
add(138, "Deep-copy a list whose nodes have next and random pointers. Inputs show [value, random-index-or-null] per node.", ([{"random": [[1, None], [2, 0], [3, 1]]}], [[1, None], [2, 0], [3, 1]]), ([{"random": []}], []), ([{"random": [[7, 0]]}], [[7, 0]]), mode="random")
add(2, "Add two nonnegative integers represented by reverse-order digits in linked lists. Return a reverse-order digit list.", ([linked([2, 4, 3]), linked([5, 6, 4])], [7, 0, 8]), ([linked([0]), linked([0])], [0]), ([linked([9, 9]), linked([1])], [0, 0, 1]))
add(141, "Return whether a linked list has a cycle. Fixture pos indicates the tail's next index, or -1 for no cycle.", ([linked([3, 2, 0, -4], 1)], True), ([linked([1])], False), ([linked([], -1)], False), ([linked([1], 0)], True))
add(287, "An array of n+1 integers contains values 1..n and one repeated value. Find it without changing the array, using constant extra space.", ([[1, 3, 4, 2, 2]], 2), ([[3, 1, 3, 4, 2]], 3), ([[1, 1]], 1))
add(146, "Implement LRUCache(capacity) with get returning -1 on a miss and put updating/evicting the least recently used item. Aim for O(1) operations.", ([design([2], ("put", [1, 10]), ("put", [2, 20]), ("get", [1]), ("put", [3, 30]), ("get", [2]), ("get", [3]))], [None, None, 10, None, -1, 30]), ([design([1], ("put", [1, 1]), ("put", [1, 2]), ("get", [1]))], [None, None, 2]), mode="design")
add(23, "Merge k sorted linked lists into one sorted linked list.", ([[linked([1, 4]), linked([1, 3]), linked([2])]], [1, 1, 2, 3, 4]), ([[]], []), ([[linked([])]], []))
add(25, "Reverse linked-list nodes in groups of k. Leave a final group shorter than k unchanged.", ([linked([1, 2, 3, 4, 5]), 2], [2, 1, 4, 3, 5]), ([linked([1, 2, 3, 4, 5]), 3], [3, 2, 1, 4, 5]), ([linked([1]), 1], [1]))
add(226, "Mirror a binary tree by swapping every node's left and right children. Return the root. Trees use level-order arrays with null gaps.", ([tree([2, 1, 3])], [2, 3, 1]), ([tree([])], []), ([tree([1, 2])], [1, None, 2]))
add(104, "Return a binary tree's maximum root-to-leaf depth; an empty tree has depth zero.", ([tree([1, 2, 3, 4])], 3), ([tree([])], 0), ([tree([1])], 1))
add(543, "Return the maximum number of edges on a path between any two nodes in a binary tree.", ([tree([1, 2, 3, 4, 5])], 3), ([tree([1])], 0), ([tree([1, 2])], 1))
add(110, "Return whether every node's left and right subtree depths differ by at most one.", ([tree([1, 2, 3])], True), ([tree([1, 2, None, 3])], False), ([tree([])], True))
add(100, "Return whether two binary trees have identical structure and values.", ([tree([1, 2]), tree([1, 2])], True), ([tree([1, 2]), tree([1, None, 2])], False), ([tree([]), tree([])], True))
add(572, "Return whether subRoot appears as an entire subtree of root with identical structure and values.", ([tree([3, 4, 5, 1, 2]), tree([4, 1, 2])], True), ([tree([1, 2, 3]), tree([2, 3])], False), ([tree([1]), tree([1])], True))
add(235, "Return the lowest common ancestor of existing nodes p and q in a binary search tree with distinct values. Fixture node values are resolved to nodes.", ([tree([6, 2, 8, 0, 4, 7, 9, None, None, 3, 5]), 2, 8], 6), ([tree([2, 1, 3]), 1, 2], 2), ([tree([2, 1]), 1, 2], 2), mode="lca")
add(102, "Return node values grouped by tree depth, from root downward and left to right.", ([tree([1, 2, 3, None, 4])], [[1], [2, 3], [4]]), ([tree([])], []), ([tree([1])], [[1]]))
add(199, "Return the rightmost visible value at each tree depth.", ([tree([1, 2, 3, None, 5, None, 4])], [1, 3, 4]), ([tree([])], []), ([tree([1, 2])], [1, 2]))
add(1448, "Count nodes whose value is at least every value on the path from the root to that node.", ([tree([3, 1, 4, 3, None, 1, 5])], 4), ([tree([1])], 1), ([tree([3, 2, 1])], 1))
add(98, "Return whether a binary tree is a strict binary search tree. The ordering applies to entire subtrees, not only immediate children.", ([tree([2, 1, 3])], True), ([tree([5, 1, 4, None, None, 3, 6])], False), ([tree([1, 1])], False))
add(230, "Return the kth smallest value in a binary search tree, with k starting at one.", ([tree([3, 1, 4, None, 2]), 2], 2), ([tree([1]), 1], 1), ([tree([2, 1, 3]), 3], 3))
add(105, "Reconstruct a binary tree from preorder and inorder traversals containing distinct values.", ([[3, 9, 20, 15, 7], [9, 3, 15, 20, 7]], [3, 9, 20, None, None, 15, 7]), ([[1], [1]], [1]), ([[1, 2], [2, 1]], [1, 2]))
add(124, "Return the largest sum on a nonempty path between any two nodes of a nonempty binary tree.", ([tree([-10, 9, 20, None, None, 15, 7])], 42), ([tree([-3])], -3), ([tree([1, 2, 3])], 6))
add(297, "Implement Codec.serialize(root) and deserialize(data) so a binary tree round-trips exactly. Any string encoding is acceptable.", ([tree([1, 2, 3, None, None, 4, 5])], [1, 2, 3, None, None, 4, 5]), ([tree([])], []), ([tree([-1])], [-1]), mode="tree_codec")
add(208, "Implement Trie.insert(word), search(word), and startsWith(prefix). Search requires a whole inserted word.", ([design([], ("insert", ["apple"]), ("search", ["app"]), ("startsWith", ["app"]), ("search", ["apple"]))], [None, False, True, True]), ([design([], ("search", ["x"]), ("insert", ["x"]), ("search", ["x"]))], [False, None, True]), mode="design")
add(211, "Implement addWord and search; '.' in a search pattern matches exactly one arbitrary letter.", ([design([], ("addWord", ["cat"]), ("search", ["c.t"]), ("search", ["ca"]), ("search", ["..."]))], [None, True, False, True]), ([design([], ("search", ["a"]))], [False]), mode="design")
add(212, "Find dictionary words on a character grid by orthogonal adjacent steps. A cell cannot be reused within one word. Return unique matches in any order.", ([[["a", "b"], ["c", "d"]], ["ab", "ac", "abc"]], ["ab", "ac"]), ([[["a"]], ["a", "aa"]], ["a"]), compare="unordered")
add(703, "Implement KthLargest(k,nums). Each add(value) returns the kth largest value seen, counting duplicates. Tests call add only when at least k values exist.", ([design([3, [4, 5, 8, 2]], ("add", [3]), ("add", [5]), ("add", [10]))], [4, 5, 5]), ([design([1, []], ("add", [-3]), ("add", [0]))], [-3, 0]), mode="design")
add(1046, "Repeatedly smash the two largest stones. Equal stones vanish; otherwise retain their weight difference. Return the final weight, or zero.", ([[2, 7, 4, 1, 8, 1]], 1), ([[2, 2]], 0), ([[5]], 5))
add(973, "Return k points nearest the origin by squared Euclidean distance, in any order. Tests avoid ties at the cutoff.", ([[[1, 3], [-2, 2]], 1], [[-2, 2]]), ([[[0, 0], [2, 1], [5, 5]], 2], [[0, 0], [2, 1]]), compare="unordered")
add(215, "Return the kth largest array element, counting duplicate values separately.", ([[3, 2, 1, 5, 6, 4], 2], 5), ([[2, 2, 1], 2], 2), ([[9], 1], 9))
add(621, "Each task takes one interval; identical task labels must have at least n intervals between executions. Return the minimum total schedule length.", ([["A", "A", "A", "B", "B", "B"], 2], 8), ([["A", "A", "B"], 0], 3), ([["A"], 4], 1))
add(355, "Implement Twitter.postTweet(user,tweet), getNewsFeed(user), follow(follower,followee), and unfollow. Feed returns at most ten newest own/followed tweet IDs, newest first.", ([design([], ("postTweet", [1, 5]), ("getNewsFeed", [1]), ("follow", [1, 2]), ("postTweet", [2, 6]), ("getNewsFeed", [1]), ("unfollow", [1, 2]), ("getNewsFeed", [1]))], [None, [5], None, None, [6, 5], None, [5]]), ([design([], ("getNewsFeed", [9]))], [[]]), mode="design")
add(295, "Implement MedianFinder.addNum(num) and findMedian() over all values added so far. Median is called only on nonempty streams.", ([design([], ("addNum", [1]), ("addNum", [2]), ("findMedian", []), ("addNum", [3]), ("findMedian", []))], [None, None, 1.5, None, 2.0]), ([design([], ("addNum", [-1]), ("findMedian", []))], [None, -1.0]), mode="design")
add(78, "Return all subsets of distinct integers, including the empty subset. Order is unrestricted.", ([[1, 2]], [[], [1], [2], [1, 2]]), ([[]], [[]]), ([[0]], [[], [0]]), compare="groups")
add(39, "Return unique combinations of positive distinct candidates summing to target. Each candidate may be reused.", ([[2, 3, 6, 7], 7], [[2, 2, 3], [7]]), ([[2], 1], []), ([[3], 6], [[3, 3]]), compare="groups")
add(46, "Return all permutations of an array of distinct integers. The order of permutations is unrestricted.", ([[1, 2]], [[1, 2], [2, 1]]), ([[0]], [[0]]), ([[1, 2, 3]], [[1, 2, 3], [1, 3, 2], [2, 1, 3], [2, 3, 1], [3, 1, 2], [3, 2, 1]]), compare="unordered")
add(90, "Return every distinct subset of an array that may contain duplicate values.", ([[1, 2, 2]], [[], [1], [2], [1, 2], [2, 2], [1, 2, 2]]), ([[0]], [[], [0]]), compare="groups")
add(40, "Return unique combinations summing to target; each occurrence in candidates may be used at most once.", ([[1, 1, 2, 5], 3], [[1, 2]]), ([[2, 5, 2, 1, 2], 5], [[1, 2, 2], [5]]), ([[2], 1], []), compare="groups")
add(79, "Return whether word can be traced through orthogonally adjacent board cells without reusing a cell.", ([[["A", "B"], ["C", "D"]], "ABD"], True), ([[["A"]], "AA"], False), ([[["A"]], "A"], True))
add(131, "Return every partition of a string into palindromic substrings. Partition order is unrestricted; substring sequence order matters.", (["aab"], [["a", "a", "b"], ["aa", "b"]]), (["a"], [["a"]]), (["aba"], [["a", "b", "a"], ["aba"]]), compare="unordered")
add(17, "Return all letter strings represented by digits 2–9 on a telephone keypad. An empty digit string gives no combinations.", (["23"], ["ad", "ae", "af", "bd", "be", "bf", "cd", "ce", "cf"]), ([""], []), (["2"], ["a", "b", "c"]), compare="unordered")
add(51, "Return all n×n boards placing n queens without shared rows, columns, or diagonals. Use 'Q' and '.', with any solution order.", ([1], [["Q"]]), ([2], []), ([4], [[".Q..", "...Q", "Q...", "..Q."], ["..Q.", "Q...", "...Q", ".Q.."]]), compare="unordered")
add(200, "Count orthogonally connected islands of '1' cells in a grid of '0' and '1'. Input mutation is permitted.", ([[list("110"), list("010"), list("001")]], 2), ([[["0"]]], 0), ([[["1"]]], 1))
add(133, "Deep-clone an undirected connected graph. Node has val and neighbors; values are 1-based adjacency-list indices. Empty input maps to None.", ([{"graph": [[2], [1, 3], [2]]}], [[2], [1, 3], [2]]), ([{"graph": [[]]}], [[]]), ([{"graph": []}], []), mode="graph")
add(695, "Return the largest orthogonally connected region of 1s in an integer grid.", ([[[1, 1, 0], [0, 1, 0], [1, 0, 1]]], 3), ([[[0]]], 0), ([[[1]]], 1))
add(417, "Return cells from which water can flow downhill or level to both the top/left boundary and bottom/right boundary. Output coordinate order is unrestricted.", ([[[1]]], [[0, 0]]), ([[[1, 2], [4, 3]]], [[0, 1], [1, 0], [1, 1]]), compare="unordered")
add(130, "Modify the board in place, turning 'O' regions fully enclosed by 'X' into 'X'. Boundary-connected regions survive.", ([[list("XXXX"), list("XOOX"), list("XXOX"), list("XOXX")]], [list("XXXX"), list("XXXX"), list("XXXX"), list("XOXX")]), ([[["O"]]], [["O"]]), mode="inplace")
add(994, "Each minute rotten oranges (2) rot orthogonally adjacent fresh oranges (1); 0 is empty. Return minutes until no fresh remain, or -1 if impossible.", ([[[2, 1, 1], [1, 1, 0], [0, 1, 1]]], 4), ([[[2, 0, 1]]], -1), ([[[0, 2]]], 0))
add(286, "Fill empty rooms (2147483647) in place with distance to the nearest gate (0), moving orthogonally around walls (-1). Unreachable rooms stay unchanged.", ([[[2147483647, -1, 0], [2147483647, 2147483647, 2147483647]]], [[4, -1, 0], [3, 2, 1]]), ([[[0]]], [[0]]), mode="inplace")
add(207, "Given numCourses and prerequisite pairs [course, prerequisite], return whether all courses can be completed.", ([2, [[1, 0]]], True), ([2, [[1, 0], [0, 1]]], False), ([3, []], True))
add(210, "Return any ordering of all course indices respecting prerequisite pairs [course, prerequisite], or [] if a cycle prevents completion.", ([3, [[2, 0], [2, 1]]], [0, 1, 2]), ([2, [[1, 0], [0, 1]]], []), ([1, []], [0]), compare="courses")
add(684, "An undirected tree on labels 1..n has one added edge. Return the last input edge whose removal restores a tree.", ([[[1, 2], [1, 3], [2, 3]]], [2, 3]), ([[[1, 2], [2, 3], [3, 4], [1, 4], [1, 5]]], [1, 4]))
add(323, "Count connected components of an undirected graph on vertices 0..n-1.", ([5, [[0, 1], [1, 2], [3, 4]]], 2), ([3, []], 3), ([1, []], 1))
add(261, "Return whether n vertices and undirected edges form a connected, cycle-free tree.", ([5, [[0, 1], [0, 2], [0, 3], [1, 4]]], True), ([3, [[0, 1], [1, 2], [2, 0]]], False), ([2, []], False), ([1, []], True))
add(127, "Return the length, including endpoints, of the shortest one-letter-at-a-time path from beginWord to endWord using wordList. Return zero if none exists.", (["hit", "cog", ["hot", "dot", "dog", "lot", "log", "cog"]], 5), (["hit", "cog", ["hot", "dot", "dog"]], 0), (["a", "c", ["a", "b", "c"]], 2))
add(332, "Use every directed ticket exactly once, starting at JFK. Return the lexicographically smallest valid itinerary; inputs have a solution.", ([[["JFK", "SFO"], ["SFO", "ATL"]]], ["JFK", "SFO", "ATL"]), ([[["JFK", "KUL"], ["JFK", "NRT"], ["NRT", "JFK"]]], ["JFK", "NRT", "JFK", "KUL"]))
add(1584, "Connect all points with minimum total Manhattan-distance edge cost.", ([[[0, 0], [2, 2], [3, 10], [5, 2], [7, 0]]], 20), ([[[1, 1]]], 0), ([[[0, 0], [1, 2]]], 3))
add(743, "Given directed weighted edges [u,v,time] with vertices 1..n, return the time for a signal from k to reach all vertices, or -1 if impossible.", ([[[2, 1, 1], [2, 3, 1], [3, 4, 1]], 4, 2], 2), ([[[1, 2, 1]], 2, 2], -1), ([[], 1, 1], 0))
add(778, "An n×n grid has elevations. At time t you may enter cells of elevation at most t. Return the earliest time to move from top-left to bottom-right orthogonally.", ([[[0, 2], [1, 3]]], 3), ([[[0]]], 0), ([[[3, 2], [0, 1]]], 3))
add(269, "Infer any valid character ordering from words sorted in an unknown alphabet, or '' for an inconsistent ordering. Include all distinct characters.", ([["wrt", "wrf", "er", "ett", "rftt"]], "wertf"), ([["z", "x", "z"]], ""), ([["abc", "ab"]], ""), ([["ab", "ac"]], "abc"), compare="alien")
add(787, "Find the cheapest directed flight cost from src to dst using at most k intermediate stops, or -1.", ([3, [[0, 1, 100], [1, 2, 100], [0, 2, 500]], 0, 2, 1], 200), ([3, [[0, 1, 100], [1, 2, 100], [0, 2, 500]], 0, 2, 0], 500), ([3, [[0, 1, 1]], 0, 2, 2], -1))
add(70, "Count ways to climb n stairs using steps of one or two.", ([2], 2), ([5], 8), ([1], 1))
add(746, "Pay cost[i] when leaving stair i; you may start at index 0 or 1 and move one or two steps. Return the minimum cost to reach beyond the last stair.", ([[10, 15, 20]], 15), ([[1, 2]], 1), ([[1, 100, 1, 1, 1, 100, 1, 1, 100, 1]], 6))
add(198, "Return the maximum sum of nonadjacent values in a row of houses with nonnegative amounts.", ([[2, 7, 9, 3, 1]], 12), ([[0]], 0), ([[2, 1, 1, 2]], 4))
add(213, "Return the maximum nonadjacent house total when the first and last houses are also adjacent in a circle.", ([[2, 3, 2]], 3), ([[1, 2, 3, 1]], 4), ([[5]], 5))
add(5, "Return a longest palindromic contiguous substring. Any maximum-length answer is accepted.", (["babad"], "bab"), (["cbbd"], "bb"), (["a"], "a"), compare="palindrome")
add(647, "Count palindromic substrings by position, including single characters.", (["aaa"], 6), (["abc"], 3), (["abba"], 6))
add(91, "Count decodings of a digit string using 1=A through 26=Z. Leading zeroes are invalid.", (["226"], 3), (["06"], 0), (["10"], 1), (["11106"], 2))
add(322, "Return the fewest coins needed for amount, with unlimited use of positive denominations, or -1 if impossible.", ([[1, 2, 5], 11], 3), ([[2], 3], -1), ([[1], 0], 0))
add(152, "Return the maximum product of a nonempty contiguous subarray.", ([[2, 3, -2, 4]], 6), ([[-2, 0, -1]], 0), ([[-2, 3, -4]], 24))
add(139, "Return whether s can be segmented into words from wordDict, reusing dictionary words if needed.", (["applepenapple", ["apple", "pen"]], True), (["catsandog", ["cats", "dog", "sand", "and", "cat"]], False), (["aaaa", ["aa"]], True))
add(300, "Return the length of a longest strictly increasing subsequence, not necessarily contiguous.", ([[10, 9, 2, 5, 3, 7, 101, 18]], 4), ([[7, 7, 7]], 1), ([[1, 2, 3]], 3))
add(416, "Return whether positive integers can be partitioned into two groups with equal sum.", ([[1, 5, 11, 5]], True), ([[1, 2, 3, 5]], False), ([[1, 1]], True))
add(62, "Count paths on an m×n grid from top-left to bottom-right moving only right or down.", ([3, 7], 28), ([1, 4], 1), ([3, 2], 3))
add(1143, "Return the length of a longest common subsequence of two strings.", (["abcde", "ace"], 3), (["abc", "def"], 0), (["", "a"], 0))
add(309, "Maximize stock profit with multiple transactions and a one-day cooldown after each sale. Hold at most one share.", ([[1, 2, 3, 0, 2]], 3), ([[1]], 0), ([[2, 1, 4]], 3))
add(518, "Count unordered combinations of unlimited coins totaling amount. Denominations are distinct positive integers.", ([5, [1, 2, 5]], 4), ([3, [2]], 0), ([0, [1]], 1))
add(494, "Assign + or - to every array element. Count assignments producing target.", ([[1, 1, 1, 1, 1], 3], 5), ([[0, 0], 0], 4), ([[1], 2], 0))
add(97, "Return whether s3 interleaves s1 and s2 while preserving the relative order within each.", (["aabcc", "dbbca", "aadbbcbcac"], True), (["aabcc", "dbbca", "aadbbbaccc"], False), (["", "", ""], True))
add(329, "Return the longest strictly increasing path through orthogonally adjacent matrix cells.", ([[[9, 9, 4], [6, 6, 8], [2, 1, 1]]], 4), ([[[1]]], 1), ([[[2, 2]]], 1))
add(115, "Count distinct subsequences of s equal to t, distinguishing choices of source positions.", (["rabbbit", "rabbit"], 3), (["babgbag", "bag"], 5), (["a", "aa"], 0))
add(72, "Return the minimum insertions, deletions, and replacements needed to transform word1 into word2.", (["horse", "ros"], 3), (["", "abc"], 3), (["same", "same"], 0))
add(312, "Burst all balloons to maximize coins. Bursting i earns left×nums[i]×right using its current neighbors, with boundary values 1.", ([[3, 1, 5, 8]], 167), ([[1, 5]], 10), ([[7]], 7))
add(10, "Match all of s against pattern p. '.' matches one character; '*' repeats the preceding element zero or more times. Patterns are valid.", (["aa", "a"], False), (["aa", "a*"], True), (["ab", ".*"], True), (["aab", "c*a*b"], True))
add(53, "Return the maximum sum of any nonempty contiguous subarray.", ([[-2, 1, -3, 4, -1, 2, 1, -5, 4]], 6), ([[-3, -1]], -1), ([[5]], 5))
add(55, "Each value is the maximum forward jump length. Return whether the final index is reachable from index zero.", ([[2, 3, 1, 1, 4]], True), ([[3, 2, 1, 0, 4]], False), ([[0]], True))
add(45, "Return the minimum jumps to reach the last index. Each value bounds the forward jump length; the destination is reachable.", ([[2, 3, 1, 1, 4]], 2), ([[0]], 0), ([[1, 1, 1]], 2))
add(134, "On a circular route, gas[i] is collected at station i and cost[i] is spent reaching the next. Return the unique feasible starting index, or -1.", ([[1, 2, 3, 4, 5], [3, 4, 5, 1, 2]], 3), ([[2, 3, 4], [3, 4, 3]], -1), ([[2], [1]], 0))
add(846, "Return whether cards can be divided into groups of groupSize consecutive values, using every card exactly once.", ([[1, 2, 3, 6, 2, 3, 4, 7, 8], 3], True), ([[1, 2, 3, 4, 5], 4], False), ([[1, 1, 2, 2], 2], True))
add(1899, "Choose triplets that can be merged componentwise by maximum to produce target exactly. Return whether possible.", ([[[2, 5, 3], [1, 8, 4], [1, 7, 5]], [2, 7, 5]], True), ([[[3, 4, 5], [4, 5, 6]], [3, 2, 5]], False), ([[[1, 1, 1]], [1, 1, 1]], True))
add(763, "Split a string into as many contiguous parts as possible so each letter appears in only one part. Return part lengths.", (["ababcbacadefegdehijhklij"], [9, 7, 8]), (["abc"], [1, 1, 1]), (["aaaa"], [4]))
add(678, "Return whether a string of parentheses and '*' can be balanced, where '*' may mean '(', ')', or empty.", (["(*)"], True), (["(*))"], True), ([")*("], False), ([""], True))
add(57, "Insert newInterval into sorted, disjoint closed intervals, merging overlaps.", ([[[1, 3], [6, 9]], [2, 5]], [[1, 5], [6, 9]]), ([[], [2, 4]], [[2, 4]]), ([[[1, 5]], [2, 3]], [[1, 5]]))
add(56, "Merge overlapping closed intervals and return them ordered by start.", ([[[1, 3], [2, 6], [8, 10], [15, 18]]], [[1, 6], [8, 10], [15, 18]]), ([[[1, 4], [4, 5]]], [[1, 5]]), ([[[2, 3]]], [[2, 3]]))
add(435, "Return the fewest intervals to remove so the rest do not overlap. Touching endpoints are allowed.", ([[[1, 2], [2, 3], [3, 4], [1, 3]]], 1), ([[[1, 2], [1, 2], [1, 2]]], 2), ([[[1, 2], [2, 3]]], 0))
add(252, "Return whether one person can attend every meeting [start,end). Meetings touching at endpoints do not overlap.", ([[[0, 30], [5, 10], [15, 20]]], False), ([[[7, 10], [2, 4]]], True), ([[]], True))
add(253, "Return the minimum simultaneous rooms needed for meetings [start,end). Endpoint-touching meetings may share a room.", ([[[0, 30], [5, 10], [15, 20]]], 2), ([[[1, 2], [2, 3]]], 1), ([[]], 0))
add(1851, "For each query return the smallest length end-start+1 of a closed interval containing it, or -1.", ([[[1, 4], [2, 4], [3, 6], [4, 4]], [2, 3, 4, 5]], [3, 3, 1, 4]), ([[[2, 3]], [1, 2, 4]], [-1, 2, -1]))
add(48, "Rotate a square matrix 90 degrees clockwise in place.", ([[[1, 2], [3, 4]]], [[3, 1], [4, 2]]), ([[[1]]], [[1]]), mode="inplace")
add(54, "Return matrix values in clockwise spiral order, beginning at top-left.", ([[[1, 2, 3], [4, 5, 6], [7, 8, 9]]], [1, 2, 3, 6, 9, 8, 7, 4, 5]), ([[[1, 2, 3]]], [1, 2, 3]), ([[[1], [2]]], [1, 2]))
add(73, "For every original matrix zero, set its entire row and column to zero in place. Aim for constant extra space.", ([[[1, 1, 1], [1, 0, 1], [1, 1, 1]]], [[1, 0, 1], [0, 0, 0], [1, 0, 1]]), ([[[0, 1], [1, 1]]], [[0, 0], [0, 1]]), mode="inplace")
add(202, "Repeatedly replace a positive integer by the sum of the squares of its decimal digits. Return whether it reaches 1.", ([19], True), ([2], False), ([1], True))
add(66, "Increment a nonnegative integer represented by most-significant-first digits, and return the resulting digits.", ([[1, 2, 9]], [1, 3, 0]), ([[9, 9]], [1, 0, 0]), ([[0]], [1]))
add(50, "Compute x raised to integer n, including negative exponents. Inputs avoid zero with a negative exponent.", ([2.0, 10], 1024.0), ([2.0, -2], 0.25), ([3.0, 0], 1.0), compare="float")
add(43, "Multiply two nonnegative decimal integer strings and return the decimal string. Avoid converting whole inputs to integers.", (["123", "45"], "5535"), (["0", "999"], "0"), (["99", "99"], "9801"))
add(2013, "Implement DetectSquares.add(point) and count(point). Count axis-aligned nonzero-area squares with the query as a corner; repeated added points count multiplicatively.", ([design([], ("add", [[3, 10]]), ("add", [[11, 2]]), ("add", [[3, 2]]), ("count", [[11, 10]]), ("add", [[11, 2]]), ("count", [[11, 10]]))], [None, None, None, 1, None, 2]), ([design([], ("count", [[0, 0]]))], [0]), mode="design")
add(136, "Every integer occurs twice except one. Return that single value with linear time and constant extra space.", ([[4, 1, 2, 1, 2]], 4), ([[0]], 0), ([[-1, 2, 2]], -1))
add(191, "Count the set bits in a nonnegative 32-bit integer.", ([11], 3), ([0], 0), ([4294967295], 32))
add(338, "For every integer from zero through n, return its number of set bits.", ([5], [0, 1, 1, 2, 1, 2]), ([0], [0]), ([2], [0, 1, 1]))
add(190, "Reverse all 32 bits of an unsigned 32-bit integer and return the unsigned result.", ([1], 2147483648), ([0], 0), ([43261596], 964176192))
add(268, "Distinct numbers are drawn from 0..n with exactly one missing. Return the missing number in linear time and constant extra space.", ([[3, 0, 1]], 2), ([[0]], 1), ([[1]], 0))
add(371, "Return the sum of two signed 32-bit integers without using + or - operators. Test sums remain in range.", ([1, 2], 3), ([-2, 3], 1), ([-4, -5], -9), ([0, 0], 0))
add(7, "Reverse a signed integer's decimal digits. Return zero if the result is outside signed 32-bit range.", ([123], 321), ([-120], -21), ([1534236469], 0), ([0], 0))

# Larger deterministic cases make the efficiency comparison more useful than
# timing only tiny examples. Expected outputs are independently specified.
for number, args, expected in [
    (217, [list(range(10000))], False),
    (217, [list(range(10000)) + [9999]], True),
    (242, ["abcdefghijklmnopqrstuvwxyz" * 400, "zyxwvutsrqponmlkjihgfedcba" * 400], True),
    (1, [list(range(2000)), 3997], [1998, 1999]),
    (128, [list(range(6000, 0, -1))], 6000),
    (3, ["abcdefghijklmnopqrstuvwxyz" * 200], 26),
    (121, [list(range(5000, 0, -1)) + [10000]], 9999),
    (20, ["(" * 1000 + ")" * 1000], True),
    (739, [list(range(3000))], [1] * 2999 + [0]),
    (704, [list(range(10000)), 9999], 9999),
    (206, [linked(list(range(300)))], list(range(299, -1, -1))),
    (200, [[["1"] * 20 for _ in range(20)]], 1),
    (323, [500, [[i, i + 1] for i in range(499)]], 1),
    (70, [35], 14930352),
    (198, [[1] * 500], 250),
    (300, [list(range(250))], 250),
    (62, [20, 20], 35345263800),
    (72, ["a" * 50, "b" * 50], 50),
]:
    EXERCISES[number]["cases"].append({"args": args, "expected": expected})
