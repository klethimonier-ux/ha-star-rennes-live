
class ClientError(Exception): pass
class ClientTimeout:
    def __init__(self,total=None): self.total=total
class ClientSession: pass
