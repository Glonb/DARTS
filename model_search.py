import  torch
from    torch import nn
import  torch.nn.functional as F
from    operations import OPS, FactorizedReduce, ReLUConvBN
from    genotypes import PRIMITIVES, Genotype


class MixedLayer(nn.Module):
    """
    a mixtures output of 8 type of units.

    we use weights to aggregate these outputs while training.
    and softmax to select the strongest edges while inference.
    """
    def __init__(self, c, stride):
        """
        :param c: 16
        :param stride: 1
        """
        super(MixedLayer, self).__init__()

        self.layers = nn.ModuleList()
        """
        PRIMITIVES = [
                    'none',
                    'max_pool_3x3',
                    'avg_pool_3x3',
                    'skip_connect',
                    'sep_conv_3x3',
                    'sep_conv_5x5',
                    'dil_conv_3x3',
                    'dil_conv_5x5'
                ]
        """
        for primitive in PRIMITIVES:
            # create corresponding layer
            layer = OPS[primitive](c, stride, False)
            # append batchnorm after pool layer
            if 'pool' in primitive:
                # disable affine w/b for batchnorm
                layer = nn.Sequential(layer, nn.BatchNorm2d(c, affine=False))

            self.layers.append(layer)

    def forward(self, x, weights):
        """
        :param x: data
        :param weights: alpha,[op_num:8], the output = sum of alpha * op(x)
        :return:
        """
        res = [w * layer(x) for w, layer in zip(weights, self.layers)]
        # element-wise add by torch.add
        res = sum(res)
        return res
        

class Cell(nn.Module):

    def __init__(self, steps, multiplier, cpp, cp, c, reduction, reduction_prev):
        """
        :param steps: 4, number of layers inside a cell
        :param multiplier: 4
        :param cpp: 48
        :param cp: 48
        :param c: 16
        :param reduction: indicates whether to reduce the output maps width
        :param reduction_prev: when previous cell reduced width, s1_d = s0_d//2
        in order to keep same shape between s1 and s0, we adopt prep0 layer to
        reduce the s0 width by half.
        """
        super(Cell, self).__init__()

        # indicating current cell is reduction or not
        self.reduction = reduction
        self.reduction_prev = reduction_prev

        # preprocess0 deal with output from prev_prev cell
        if reduction_prev:
            # if prev cell has reduced channel/double width,
            # it will reduce width by half
            self.preprocess0 = FactorizedReduce(cpp, c, affine=False)
        else:
            self.preprocess0 = ReLUConvBN(cpp, c, 1, 1, 0, affine=False)
        # preprocess1 deal with output from prev cell
        self.preprocess1 = ReLUConvBN(cp, c, 1, 1, 0, affine=False)

        # steps inside a cell
        self.steps = steps # 4
        self.multiplier = multiplier # 4

        self.layers = nn.ModuleList()

        for i in range(self.steps):
            # for each i inside cell, it connects with all previous output
            # plus previous two cells' output
            for j in range(2 + i):
                # for reduction cell, it will reduce the heading 2 inputs only
                stride = 2 if reduction and j < 2 else 1
                layer = MixedLayer(c, stride)
                self.layers.append(layer)
        '''
        self.layers[0,1]代表的是内部节点0的前继操作
        self.layers[2,3,4]代表的是内部节点1的前继操作
        self.layers[5,6,7,8]代表的是内部节点2的前继操作
        self.layers[9,10,11,12,13]代表的是内部节点3的前继操作
        '''

    def forward(self, s0, s1, weights):
        """
        :param s0:
        :param s1:
        :param weights: [14, 8]
        :return:
        """
        # print('s0:', s0.shape,end='=>')
        s0 = self.preprocess0(s0) # [40, 48, 32, 32], [40, 16, 32, 32]
        # print(s0.shape, self.reduction_prev)
        # print('s1:', s1.shape,end='=>')
        s1 = self.preprocess1(s1) # [40, 48, 32, 32], [40, 16, 32, 32]
        # print(s1.shape)

        states = [s0, s1]
        offset = 0
        # for each node, receive input from all previous intermediate nodes and s0, s1
        for i in range(self.steps): # 4
            # [40, 16, 32, 32]
            s = sum(self.layers[offset + j](h, weights[offset + j]) for j, h in enumerate(states))
            offset += len(states)
            # append one state since s is the elem-wise addition of all output
            states.append(s)
            # print('node:',i, s.shape, self.reduction)

        # concat along dim=channel
        return torch.cat(states[-self.multiplier:], dim=1) # 6 of [40, 16, 32, 32]


class Network(nn.Module):
    """
    stack number:layer of cells and then flatten to fed a linear layer
    """
    def __init__(self, c, num_classes, layers, criterion, steps=4, multiplier=4, stem_multiplier=3):
        """
        :param c: 16
        :param num_classes: 10
        :param layers: number of cells of current network
        :param criterion:
        :param steps: nodes num inside cell
        :param multiplier: output channel of cell = multiplier * ch
        :param stem_multiplier: output channel of stem net = stem_multiplier * ch
        """
        super(Network, self).__init__()

        self.c = c
        self.num_classes = num_classes
        self.layers = layers
        self.criterion = criterion
        self.steps = steps
        self.multiplier = multiplier


        # stem_multiplier is for stem network,
        # and multiplier is for general cell
        c_curr = stem_multiplier * c # 3*16
        # stem network, convert 3 channel to c_curr
        self.stem = nn.Sequential( # 3 => 48
            nn.Conv2d(3, c_curr, 3, padding=1, bias=False),
            nn.BatchNorm2d(c_curr)
        )

        # c_curr means a factor of the output channels of current cell
        # output channels = multiplier * c_curr
        cpp, cp, c_curr = c_curr, c_curr, c # 48, 48, 16
        self.cells = nn.ModuleList()
        reduction_prev = False
        for i in range(layers):

            # for layer in the middle [1/3, 2/3], reduce via stride=2
            if i in [layers // 3, 2 * layers // 3]:
                c_curr *= 2
                reduction = True
            else:
                reduction = False

            # [cp, h, h] => [multiplier*c_curr, h/h//2, h/h//2]
            # the output channels = multiplier * c_curr
            cell = Cell(steps, multiplier, cpp, cp, c_curr, reduction, reduction_prev)
            # update reduction_prev
            reduction_prev = reduction

            self.cells += [cell]

            cpp, cp = cp, multiplier * c_curr

        # adaptive pooling output size to 1x1
        self.global_pooling = nn.AdaptiveAvgPool2d(1)
        # since cp records last cell's output channels
        # it indicates the input channel number
        self.classifier = nn.Linear(cp, num_classes)

        # k is the total number of edges inside single cell, 14
        k = sum(1 for i in range(self.steps) for j in range(2 + i))
        num_ops = len(PRIMITIVES) # 8

        # TODO
        # this kind of implementation will add alpha into self.parameters()
        # it has num k of alpha parameters, and each alpha shape: [num_ops]
        # it requires grad and can be converted to cpu/gpu automatically
        self.alpha_normal = nn.Parameter(torch.randn(k, num_ops))
        self.alpha_reduce = nn.Parameter(torch.randn(k, num_ops))
        with torch.no_grad():
            # initialize to smaller value
            self.alpha_normal.mul_(1e-3)
            self.alpha_reduce.mul_(1e-3)
        self._arch_parameters = [
            self.alpha_normal,
            self.alpha_reduce,
        ]

    def new(self):
        """
        create a new model and initialize it with current alpha parameters.
        However, its weights are left untouched.
        :return:
        """
        model_new = Network(self.c, self.num_classes, self.layers, self.criterion).cuda()
        for x, y in zip(model_new.arch_parameters(), self.arch_parameters()):
            x.data.copy_(y.data)
        return model_new

    def forward(self, x):
        """
        in: torch.Size([3, 3, 32, 32])
        stem: torch.Size([3, 48, 32, 32])
        cell: 0 torch.Size([3, 64, 32, 32]) False
        cell: 1 torch.Size([3, 64, 32, 32]) False
        cell: 2 torch.Size([3, 128, 16, 16]) True
        cell: 3 torch.Size([3, 128, 16, 16]) False
        cell: 4 torch.Size([3, 128, 16, 16]) False
        cell: 5 torch.Size([3, 256, 8, 8]) True
        cell: 6 torch.Size([3, 256, 8, 8]) False
        cell: 7 torch.Size([3, 256, 8, 8]) False
        pool:   torch.Size([16, 256, 1, 1])
        linear: [b, 10]
        :param x:
        :return:
        """
        # print('in:', x.shape)
        # s0 & s1 means the last cells' output
        s0 = s1 = self.stem(x) # [b, 3, 32, 32] => [b, 48, 32, 32]
        # print('stem:', s0.shape)

        for i, cell in enumerate(self.cells):
            # weights are shared across all reduction cell or normal cell
            # according to current cell's type, it choose which architecture parameters
            # to use
            if cell.reduction: # if current cell is reduction cell
                weights = F.softmax(self.alpha_reduce, dim=-1)
            else:
                weights = F.softmax(self.alpha_normal, dim=-1) # [14, 8]
            # execute cell() firstly and then assign s0=s1, s1=result
            s0, s1 = s1, cell(s0, s1, weights) # [40, 64, 32, 32]
            # print('cell:',i, s1.shape, cell.reduction, cell.reduction_prev)
            # print('\n')

        # s1 is the last cell's output
        out = self.global_pooling(s1)
        # print('pool', out.shape)
        logits = self.classifier(out.view(out.size(0), -1))

        return logits

    def loss(self, x, target):
        """
        :param x:
        :param target:
        :return:
        """
        logits = self(x)
        return self.criterion(logits, target)

    def arch_parameters(self):
        return self._arch_parameters

    def genotype(self):
        """
        :return:
        """
        def _parse(weights):
            """
            :param weights: [14, 8]
            :return:
            """
            gene = []
            n = 2
            start = 0
            for i in range(self.steps): # for each node
                end = start + n
                W = weights[start:end].copy() # [2, 8], [3, 8], ...
                edges = sorted(range(i + 2), # i+2 is the number of connection for node i
                            key=lambda x: -max(W[x][k] # by descending order
                                               for k in range(len(W[x])) # get strongest ops
                                               if k != PRIMITIVES.index('none'))
                               )[:2] # only has two inputs
                for j in edges: # for every input nodes j of current node i
                    k_best = None
                    for k in range(len(W[j])): # get strongest ops for current input j->i
                        if k != PRIMITIVES.index('none'):
                            if k_best is None or W[j][k] > W[j][k_best]:
                                k_best = k
                    gene.append((PRIMITIVES[k_best], j)) # save ops and input node
                start = end
                n += 1
            return gene

        gene_normal = _parse(F.softmax(self.alpha_normal, dim=-1).data.cpu().numpy())
        gene_reduce = _parse(F.softmax(self.alpha_reduce, dim=-1).data.cpu().numpy())

        concat = range(2 + self.steps - self.multiplier, self.steps + 2)
        genotype = Genotype(
            normal=gene_normal, normal_concat=concat,
            reduce=gene_reduce, reduce_concat=concat
        )

        return genotype
